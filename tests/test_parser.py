import io
import unittest

from bjoern import _parser
from bjoern._parser import ParseError, RequestParser


def parse(data, remote_addr='1.2.3.4'):
    p = RequestParser(remote_addr)
    return p.feed(data)


class TestParser(unittest.TestCase):
    def test_simple_get(self):
        ev = parse(b'GET /foo?a=b HTTP/1.1\r\nHost: x\r\n\r\n')
        [req] = ev
        self.assertEqual(req[0], 'request')
        env = req[1]
        self.assertEqual(env['REQUEST_METHOD'], 'GET')
        self.assertEqual(env['PATH_INFO'], '/foo')
        self.assertEqual(env['QUERY_STRING'], 'a=b')
        self.assertEqual(env['HTTP_HOST'], 'x')
        self.assertEqual(env['SERVER_PROTOCOL'], 'HTTP/1.1')
        self.assertEqual(env['REMOTE_ADDR'], '1.2.3.4')
        self.assertIsInstance(env['wsgi.input'], io.BytesIO)
        self.assertTrue(req[2])  # keep-alive

    def test_query_string_always_set(self):
        [req] = parse(b'GET /foo HTTP/1.1\r\n\r\n')
        self.assertEqual(req[1]['QUERY_STRING'], '')

    def test_incremental_feed(self):
        p = RequestParser('')
        self.assertEqual(p.feed(b'GET / HT'), [])
        self.assertEqual(p.feed(b'TP/1.1\r\nHost: x'), [])
        ev = p.feed(b'\r\n\r\n')
        self.assertEqual(ev[0][1]['PATH_INFO'], '/')

    def test_lf_line_endings(self):
        [req] = parse(b'GET / HTTP/1.1\nHost: x\n\n')
        self.assertEqual(req[1]['HTTP_HOST'], 'x')

    def test_absolute_form(self):
        [req] = parse(b'GET http://example.com/p?q=1 HTTP/1.1\r\n\r\n')
        self.assertEqual(req[1]['PATH_INFO'], '/p')
        self.assertEqual(req[1]['QUERY_STRING'], 'q=1')

    def test_fragment_stripped(self):
        [req] = parse(b'GET /p#frag?x HTTP/1.1\r\n\r\n')
        self.assertEqual(req[1]['PATH_INFO'], '/p')

    def test_percent_decoding(self):
        [req] = parse(b'GET /a%20b%c3%a9 HTTP/1.1\r\n\r\n')
        self.assertEqual(req[1]['PATH_INFO'], '/a b\xc3\xa9')

    def test_bad_percent(self):
        for target in (b'/a%2', b'/a%zz', b'/a%'):
            with self.assertRaises(ParseError) as cm:
                parse(b'GET %s HTTP/1.1\r\n\r\n' % target)
            self.assertEqual(cm.exception.code, _parser.BAD_REQUEST)

    def test_bad_version(self):
        with self.assertRaises(ParseError):
            parse(b'GET / HTTP/2.0\r\n\r\n')

    def test_underscore_header_dropped(self):
        [req] = parse(b'GET / HTTP/1.1\r\nFoo_Bar: bad\r\nX-Ok: y\r\n\r\n')
        env = req[1]
        self.assertNotIn('HTTP_FOO_BAR', env)
        self.assertEqual(env['HTTP_X_OK'], 'y')

    def test_duplicate_headers_joined(self):
        [req] = parse(b'GET / HTTP/1.1\r\nX-A: 1\r\nX-A: 2\r\n\r\n')
        self.assertEqual(req[1]['HTTP_X_A'], '1,2')

    def test_obs_fold_rejected(self):
        with self.assertRaises(ParseError):
            parse(b'GET / HTTP/1.1\r\nX-A: 1\r\n more\r\n\r\n')

    def test_header_no_colon(self):
        with self.assertRaises(ParseError):
            parse(b'GET / HTTP/1.1\r\nBadHeader\r\n\r\n')

    def test_content_length(self):
        [req] = parse(b'POST / HTTP/1.1\r\nContent-Length: 5\r\n\r\nhello')
        env = req[1]
        self.assertEqual(env['CONTENT_LENGTH'], '5')
        self.assertNotIn('HTTP_CONTENT_LENGTH', env)
        self.assertEqual(env['wsgi.input'].read(), b'hello')

    def test_content_length_partial(self):
        p = RequestParser('')
        self.assertEqual(
            p.feed(b'POST / HTTP/1.1\r\nContent-Length: 5\r\n\r\nhe'), [])
        [req] = p.feed(b'llo')
        self.assertEqual(req[1]['wsgi.input'].read(), b'hello')

    def test_bad_content_length(self):
        for cl in (b'abc', b'-5', b'5x', b'\xb2'):
            with self.assertRaises(ParseError):
                parse(b'POST / HTTP/1.1\r\nContent-Length: %s\r\n\r\n' % cl)

    def test_bad_chunk_sizes(self):
        for size in (b'-1', b'0x10', b'1_0', b'+5', b'', b' 5', b'1' * 17):
            with self.assertRaises(ParseError) as cm:
                parse(b'POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n'
                      + size + b'\r\n')
            self.assertEqual(cm.exception.code, _parser.BAD_REQUEST)

    def test_leading_crlf_tolerated(self):
        [req] = parse(b'\r\n\r\nGET / HTTP/1.1\r\n\r\n')
        self.assertEqual(req[1]['PATH_INFO'], '/')

    def test_pipelined_stray_crlf(self):
        p = RequestParser('')
        ev = p.feed(b'GET /a HTTP/1.1\r\n\r\n\r\nGET /b HTTP/1.1\r\n\r\n')
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[1][1]['PATH_INFO'], '/b')

    def test_oversized_header_block_with_terminator(self):
        with self.assertRaises(ParseError) as cm:
            parse(b'GET / HTTP/1.1\r\nX: ' + b'x' * (81 * 1024) + b'\r\n\r\n')
        self.assertEqual(cm.exception.code, _parser.BAD_REQUEST)

    def test_chunked_body(self):
        [req] = parse(b'POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n'
                      b'5\r\nhello\r\n6;ext=1\r\n world\r\n0\r\nX-T: t\r\n\r\n')
        self.assertEqual(req[1]['wsgi.input'].read(), b'hello world')

    def test_te_and_cl_conflict(self):
        with self.assertRaises(ParseError):
            parse(b'POST / HTTP/1.1\r\nContent-Length: 1\r\n'
                  b'Transfer-Encoding: chunked\r\n\r\n')

    def test_te_not_chunked(self):
        with self.assertRaises(ParseError) as cm:
            parse(b'POST / HTTP/1.1\r\nTransfer-Encoding: gzip\r\n\r\n')
        self.assertEqual(cm.exception.code, _parser.LENGTH_REQUIRED)

    def test_keep_alive_rules(self):
        [req] = parse(b'GET / HTTP/1.1\r\n\r\n')
        self.assertTrue(req[2])
        [req] = parse(b'GET / HTTP/1.1\r\nConnection: close\r\n\r\n')
        self.assertFalse(req[2])
        [req] = parse(b'GET / HTTP/1.0\r\n\r\n')
        self.assertFalse(req[2])
        [req] = parse(b'GET / HTTP/1.0\r\nConnection: keep-alive\r\n\r\n')
        self.assertTrue(req[2])
        [req] = parse(b'GET / HTTP/1.0\r\nConnection: keep-alive\r\n\r\n')
        self.assertEqual(req[1]['SERVER_PROTOCOL'], 'HTTP/1.0')

    def test_expect_continue(self):
        p = RequestParser('')
        ev = p.feed(b'POST / HTTP/1.1\r\nContent-Length: 3\r\n'
                    b'Expect: 100-Continue\r\n\r\n')
        self.assertEqual(ev, [('expect_continue',)])
        [req] = p.feed(b'abc')
        self.assertEqual(req[1]['wsgi.input'].read(), b'abc')

    def test_expect_bad_value(self):
        with self.assertRaises(ParseError) as cm:
            parse(b'POST / HTTP/1.1\r\nExpect: nonsense\r\n\r\n')
        self.assertEqual(cm.exception.code, _parser.EXPECTATION_FAILED)

    def test_expect_body_already_sent(self):
        # Body in the same packet: no 100-continue is emitted.
        [req] = parse(b'POST / HTTP/1.1\r\nContent-Length: 3\r\n'
                      b'Expect: 100-continue\r\n\r\nabc')
        self.assertEqual(req[0], 'request')

    def test_pipelined(self):
        p = RequestParser('')
        ev = p.feed(b'GET /a HTTP/1.1\r\n\r\nGET /b HTTP/1.1\r\n\r\n')
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[0][1]['PATH_INFO'], '/a')
        self.assertEqual(ev[1][1]['PATH_INFO'], '/b')

    def test_header_size_limit(self):
        with self.assertRaises(ParseError):
            parse(b'GET / HTTP/1.1\r\n' + b'x' * (81 * 1024))


if __name__ == '__main__':
    unittest.main()
