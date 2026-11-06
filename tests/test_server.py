import io
import socket
import threading
import time
import unittest

import bjoern


def serve(app):
    """Start a server for `app` on an ephemeral port (daemon thread)."""
    sock = bjoern.bind_and_listen('127.0.0.1', 0)
    port = sock.getsockname()[1]
    t = threading.Thread(target=bjoern.server_run, args=(sock, app),
                         daemon=True)
    t.start()
    return port


def connect(port):
    s = socket.create_connection(('127.0.0.1', port), timeout=5)
    return s


def read_to_close(s):
    data = b''
    while True:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk
    return data


def fetch(port, request, shutdown_write=True):
    s = connect(port)
    s.sendall(request)
    if shutdown_write:
        s.shutdown(socket.SHUT_WR)
    s.settimeout(5)
    data = read_to_close(s)
    s.close()
    return data


def read_headers(s):
    """Read until end of response headers; return (header_block, rest)."""
    data = b''
    while b'\r\n\r\n' not in data:
        chunk = s.recv(65536)
        assert chunk, 'connection closed before headers'
        data += chunk
    head, _, rest = data.partition(b'\r\n\r\n')
    return head + b'\r\n\r\n', rest


def ok_app(body=b'hello', headers=None):
    def app(environ, start_response):
        start_response('200 OK', headers or
                       [('Content-Length', str(len(body)))])
        return [body]
    return app


class TestServer(unittest.TestCase):
    def test_simple_get(self):
        port = serve(ok_app(b'hello'))
        self.assertEqual(
            fetch(port, b'GET / HTTP/1.1\r\n\r\n'),
            b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\n'
            b'Connection: Keep-Alive\r\n\r\nhello')

    def test_environ(self):
        seen = []

        def app(environ, start_response):
            seen.append(dict(environ))
            start_response('200 OK', [('Content-Length', '0')])
            return [b'']

        port = serve(app)
        fetch(port, b'POST /a%20b?q=1&r=2 HTTP/1.1\r\nHost: h\r\n'
                    b'X-Test: v\r\nContent-Length: 4\r\n'
                    b'Content-Type: text/x\r\n\r\nbody')
        [env] = seen
        self.assertEqual(env['PATH_INFO'], '/a b')
        self.assertEqual(env['QUERY_STRING'], 'q=1&r=2')
        self.assertEqual(env['REQUEST_METHOD'], 'POST')
        self.assertEqual(env['SERVER_PROTOCOL'], 'HTTP/1.1')
        self.assertEqual(env['HTTP_HOST'], 'h')
        self.assertEqual(env['HTTP_X_TEST'], 'v')
        self.assertEqual(env['CONTENT_LENGTH'], '4')
        self.assertEqual(env['CONTENT_TYPE'], 'text/x')
        self.assertNotIn('HTTP_CONTENT_LENGTH', env)
        self.assertNotIn('HTTP_CONTENT_TYPE', env)
        self.assertEqual(env['SERVER_NAME'], '127.0.0.1')
        self.assertEqual(env['SERVER_PORT'], str(port))
        self.assertEqual(env['REMOTE_ADDR'], '127.0.0.1')
        self.assertEqual(env['SCRIPT_NAME'], '')
        self.assertEqual(env['wsgi.version'], (1, 0))
        self.assertIs(env['wsgi.file_wrapper'], bjoern.FileWrapper)
        self.assertIs(env['wsgi.input_terminated'], True)
        self.assertEqual(env['wsgi.url_scheme'], 'http')
        self.assertIs(env['wsgi.multithread'], False)
        self.assertIs(env['wsgi.multiprocess'], True)
        self.assertIs(env['wsgi.run_once'], False)
        self.assertEqual(env['wsgi.input'].read(), b'body')

    def test_underscore_header_dropped(self):
        seen = []

        def app(environ, start_response):
            seen.append('HTTP_FOO_BAR' in environ)
            start_response('200 OK', [('Content-Length', '0')])
            return [b'']

        port = serve(app)
        fetch(port, b'GET / HTTP/1.1\r\nFoo_Bar: bad\r\n\r\n')
        self.assertEqual(seen, [False])

    def test_duplicate_headers_joined(self):
        seen = []

        def app(environ, start_response):
            seen.append(environ.get('HTTP_X_A'))
            start_response('200 OK', [('Content-Length', '0')])
            return [b'']

        port = serve(app)
        fetch(port, b'GET / HTTP/1.1\r\nX-A: 1\r\nX-A: 2\r\n\r\n')
        self.assertEqual(seen, ['1,2'])

    def test_bad_content_length_latin1_digit(self):
        port = serve(ok_app(b'hi'))
        self.assertEqual(
            fetch(port, b'POST / HTTP/1.1\r\nContent-Length: \xb2\r\n\r\n'),
            b'HTTP/1.1 400 Bad Request\r\n\r\n')
        # The server survived: a fresh connection still gets a 200.
        self.assertEqual(
            fetch(port, b'GET / HTTP/1.1\r\n\r\n'),
            b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n'
            b'Connection: Keep-Alive\r\n\r\nhi')

    def test_bad_request_exact_bytes(self):
        port = serve(ok_app())
        self.assertEqual(
            fetch(port, b'GET /%20%20% HTTP/1.1\r\n\r\n'),
            b'HTTP/1.1 400 Bad Request\r\n\r\n')

    def test_post_body(self):
        seen = []

        def app(environ, start_response):
            seen.append(environ['wsgi.input'].read())
            start_response('200 OK', [('Content-Length', '0')])
            return [b'']

        port = serve(app)
        fetch(port, b'POST / HTTP/1.1\r\nContent-Length: 11\r\n\r\nhello world')
        self.assertEqual(seen, [b'hello world'])

    def test_chunked_request_body(self):
        seen = []

        def app(environ, start_response):
            seen.append(environ['wsgi.input'].read())
            start_response('200 OK', [('Content-Length', '0')])
            return [b'']

        port = serve(app)
        fetch(port, b'POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n'
                    b'5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n')
        self.assertEqual(seen, [b'hello world'])

    def test_http11_unknown_length_chunked(self):
        def app(environ, start_response):
            start_response('200 OK', [])
            return [b'aa', b'bb']

        port = serve(app)
        self.assertEqual(
            fetch(port, b'GET / HTTP/1.1\r\n\r\n'),
            b'HTTP/1.1 200 OK\r\nConnection: Keep-Alive\r\n'
            b'Transfer-Encoding: chunked\r\n\r\n'
            b'2\r\naa\r\n2\r\nbb\r\n0\r\n\r\n')

    def test_http10_unknown_length_close(self):
        def app(environ, start_response):
            start_response('200 OK', [])
            return [b'aa']

        port = serve(app)
        self.assertEqual(
            fetch(port, b'GET / HTTP/1.0\r\n\r\n'),
            b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\naa')

    def test_connection_close(self):
        port = serve(ok_app(b'x'))
        self.assertEqual(
            fetch(port, b'GET / HTTP/1.1\r\nConnection: close\r\n\r\n'),
            b'HTTP/1.1 200 OK\r\nContent-Length: 1\r\n'
            b'Connection: close\r\n\r\nx')

    def test_pipelined_requests(self):
        port = serve(ok_app(b'hi'))
        s = connect(port)
        s.sendall(b'GET /a HTTP/1.1\r\n\r\nGET /b HTTP/1.1\r\n'
                  b'Connection: close\r\n\r\n')
        s.settimeout(5)
        data = read_to_close(s)
        s.close()
        expected = (b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n'
                    b'Connection: Keep-Alive\r\n\r\nhi'
                    b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n'
                    b'Connection: close\r\n\r\nhi')
        self.assertEqual(data, expected)

    def test_generator_lazy_start_response(self):
        def app(environ, start_response):
            yield b''
            start_response('200 OK', [('Content-Length', '3')])
            yield b'abc'

        port = serve(app)
        self.assertEqual(
            fetch(port, b'GET / HTTP/1.1\r\n\r\n'),
            b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\n'
            b'Connection: Keep-Alive\r\n\r\nabc')

    def test_app_exception_500(self):
        def app(environ, start_response):
            raise ValueError('boom')

        port = serve(app)
        self.assertEqual(
            fetch(port, b'GET / HTTP/1.1\r\n\r\n'),
            b'HTTP/1.1 500 Internal Server Error\r\n\r\n')

    def test_start_response_not_called(self):
        def app(environ, start_response):
            return [b'x']

        port = serve(app)
        self.assertEqual(
            fetch(port, b'GET / HTTP/1.1\r\n\r\n'),
            b'HTTP/1.1 500 Internal Server Error\r\n\r\n')

    def test_start_response_called_twice(self):
        def app(environ, start_response):
            start_response('200 OK', [])
            start_response('200 OK', [])
            return [b'x']

        port = serve(app)
        self.assertEqual(
            fetch(port, b'GET / HTTP/1.1\r\n\r\n'),
            b'HTTP/1.1 500 Internal Server Error\r\n\r\n')

    def test_bad_header_types(self):
        def app(environ, start_response):
            start_response('200 OK', [('X-A', 1)])
            return [b'x']

        port = serve(app)
        self.assertEqual(
            fetch(port, b'GET / HTTP/1.1\r\n\r\n'),
            b'HTTP/1.1 500 Internal Server Error\r\n\r\n')

    def test_exc_info_after_headers_sent_aborts(self):
        def app(environ, start_response):
            start_response('200 OK', [])
            yield b'first'
            try:
                raise ValueError('late')
            except ValueError:
                import sys
                start_response('500 X', [], sys.exc_info())
            yield b'never'

        port = serve(app)
        s = connect(port)
        s.sendall(b'GET / HTTP/1.1\r\n\r\n')
        s.settimeout(5)
        data = read_to_close(s)
        s.close()
        # First chunk was sent, then the connection was aborted (no
        # terminating chunk).
        self.assertTrue(data.startswith(b'HTTP/1.1 200 OK\r\n'))
        self.assertIn(b'5\r\nfirst\r\n', data)
        self.assertNotIn(b'0\r\n\r\n', data)
        self.assertNotIn(b'never', data)

    def test_exc_info_before_headers_resends(self):
        def app(environ, start_response):
            start_response('200 OK', [])
            try:
                raise ValueError('early')
            except ValueError:
                import sys
                start_response('500 Error',
                               [('Content-Length', '3')], sys.exc_info())
            return [b'bad']

        port = serve(app)
        self.assertEqual(
            fetch(port, b'GET / HTTP/1.1\r\n\r\n'),
            b'HTTP/1.1 500 Error\r\nContent-Length: 3\r\n'
            b'Connection: Keep-Alive\r\n\r\nbad')

    def test_iterable_close_called_once(self):
        closed = []

        class Body:
            def __iter__(self):
                yield b'a'
                yield b'b'

            def close(self):
                closed.append(1)

        def app(environ, start_response):
            start_response('200 OK', [])
            return Body()

        port = serve(app)
        fetch(port, b'GET / HTTP/1.1\r\n\r\n')
        self.assertEqual(closed, [1])

    def test_iterable_close_on_error(self):
        closed = []

        class Body:
            def __iter__(self):
                yield b'a'
                raise ValueError('mid-stream')

            def close(self):
                closed.append(1)

        def app(environ, start_response):
            start_response('200 OK', [])
            return Body()

        port = serve(app)
        s = connect(port)
        s.sendall(b'GET / HTTP/1.1\r\n\r\n')
        s.settimeout(5)
        data = read_to_close(s)
        s.close()
        self.assertIn(b'1\r\na\r\n', data)
        self.assertEqual(closed, [1])

    def test_204_not_chunked(self):
        def app(environ, start_response):
            start_response('204 No Content', [])
            return [b'']

        port = serve(app)
        self.assertEqual(
            fetch(port, b'GET / HTTP/1.1\r\n\r\n'),
            b'HTTP/1.1 204 No Content\r\nConnection: Keep-Alive\r\n\r\n')

    def test_file_wrapper_sendfile(self):
        import os
        import tempfile
        fd, path = tempfile.mkstemp()
        content = b'x' * 100000
        os.write(fd, content)
        os.lseek(fd, 0, 0)

        def app(environ, start_response):
            start_response('200 OK',
                           [('Content-Length', str(len(content)))])
            return environ['wsgi.file_wrapper'](os.fdopen(fd, 'rb'))

        port = serve(app)
        try:
            s = connect(port)
            s.sendall(b'GET / HTTP/1.1\r\nConnection: close\r\n\r\n')
            s.settimeout(10)
            data = read_to_close(s)
            s.close()
            head, _, body = data.partition(b'\r\n\r\n')
            self.assertIn(b'Content-Length: ' + str(len(content)).encode(),
                          head)
            self.assertEqual(body, content)
        finally:
            os.unlink(path)

    def test_file_wrapper_no_fd(self):
        class PseudoFile:
            def read(self, n=None):
                return b''

            def fileno(self):
                raise io.UnsupportedOperation()

        def app(environ, start_response):
            start_response('200 OK', [('Content-Length', '0')])
            return environ['wsgi.file_wrapper'](PseudoFile())

        port = serve(app)
        self.assertEqual(
            fetch(port, b'GET / HTTP/1.1\r\n\r\n'),
            b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n'
            b'Connection: Keep-Alive\r\n\r\n')

    def test_expect_100_continue(self):
        port = serve(ok_app(b'ok'))
        s = connect(port)
        s.sendall(b'POST / HTTP/1.1\r\nContent-Length: 3\r\n'
                  b'Expect: 100-continue\r\n\r\n')
        s.settimeout(5)
        resp = s.recv(4096)
        self.assertEqual(resp, b'HTTP/1.1 100 Continue\r\n\r\n')
        s.sendall(b'abc')
        head, rest = read_headers(s)
        s.close()
        self.assertIn(b'200 OK', head)

    def test_expect_bad_exact_417(self):
        port = serve(ok_app())
        self.assertEqual(
            fetch(port, b'POST / HTTP/1.1\r\nContent-Length: 3\r\n'
                        b'Expect: badness\r\n\r\n'),
            b'HTTP/1.1 417 Expectation Failed\r\n\r\n')

    def test_expect_body_already_sent(self):
        port = serve(ok_app(b'ok'))
        s = connect(port)
        s.sendall(b'POST / HTTP/1.1\r\nContent-Length: 3\r\n'
                  b'Expect: 100-continue\r\n\r\nabc')
        s.settimeout(5)
        head, rest = read_headers(s)
        s.close()
        self.assertIn(b'200 OK', head)
        self.assertNotIn(b'100 Continue', head + rest)

    def test_large_response_slow_reader(self):
        body = b'z' * (3 * 1024 * 1024)
        chunk = body[:65536]
        nchunks = len(body) // len(chunk)

        def app(environ, start_response):
            start_response('200 OK', [('Content-Length', str(len(body)))])
            for _ in range(nchunks):
                yield chunk

        port = serve(app)
        s = connect(port)
        s.sendall(b'GET / HTTP/1.1\r\nConnection: close\r\n\r\n')
        s.settimeout(15)
        head, rest = read_headers(s)
        got = len(rest)
        while got < len(body):
            time.sleep(0.05)
            data = s.recv(65536)
            if not data:
                break
            got += len(data)
        s.close()
        self.assertEqual(got, len(body))


if __name__ == '__main__':
    unittest.main()
