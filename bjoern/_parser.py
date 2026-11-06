import io

MAX_HEADER_SIZE = 80 * 1024

BAD_REQUEST = 1
LENGTH_REQUIRED = 2
EXPECTATION_FAILED = 3
SERVER_ERROR = 4

ERROR_MESSAGES = {
    BAD_REQUEST: b"HTTP/1.1 400 Bad Request\r\n\r\n",
    LENGTH_REQUIRED: b"HTTP/1.1 406 Length Required\r\n\r\n",
    EXPECTATION_FAILED: b"HTTP/1.1 417 Expectation Failed\r\n\r\n",
    SERVER_ERROR: b"HTTP/1.1 500 Internal Server Error\r\n\r\n",
}

CONTINUE = b"HTTP/1.1 100 Continue\r\n\r\n"

_AGAIN = object()


class ParseError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


_HEX = b"0123456789abcdefABCDEF"
_TOKEN = frozenset(b"!#$%&'*+-.^_`|~0123456789"
                   b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _unquote(data):
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        c = data[i]
        if c == 0x25:  # '%'
            if i + 2 >= n or data[i + 1] not in _HEX or data[i + 2] not in _HEX:
                raise ParseError(BAD_REQUEST)
            out.append(int(data[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(c)
            i += 1
    return bytes(out)


def _header_end(buf):
    a = buf.find(b"\r\n\r\n")
    b = buf.find(b"\n\n")
    if a == -1:
        return (b, 2) if b != -1 else None
    if b == -1:
        return (a, 4)
    return (b, 2) if b < a else (a, 4)


class RequestParser:
    """Incremental HTTP/1.x request parser. feed() bytes, get events back.

    Events: ('expect_continue',) or ('request', environ, keep_alive).
    Raises ParseError(code) on malformed input.
    """

    def __init__(self, remote_addr=''):
        self.buf = bytearray()
        self.remote_addr = remote_addr
        self.reset()

    def reset(self):
        self.state = 'headers'
        self.environ = None
        self.keep_alive = False
        self.expect_continue = False
        self.body_remaining = 0
        self.body_parts = None
        self.http_minor = 1

    def feed(self, data):
        if data:
            self.buf += data
        events = []
        while True:
            if self.state == 'headers':
                ev = self._parse_headers()
            elif self.state == 'content-length':
                ev = self._parse_content_length()
            elif self.state == 'chunked':
                ev = self._parse_chunked()
            else:
                ev = None
            if ev is None:
                return events
            if ev is _AGAIN:
                continue
            events.append(ev)
            if ev[0] == 'request':
                self.reset()

    def _parse_headers(self):
        # Tolerate stray CRLFs between requests (http-parser parity).
        buf = self.buf
        i = 0
        while i < len(buf) and buf[i] in (0x0d, 0x0a):
            i += 1
        if i:
            del buf[:i]
            if not buf:
                return None
        found = _header_end(self.buf)
        if found is None:
            if len(self.buf) > MAX_HEADER_SIZE:
                raise ParseError(BAD_REQUEST)
            return None
        end, tlen = found
        if end > MAX_HEADER_SIZE:
            raise ParseError(BAD_REQUEST)
        block = bytes(self.buf[:end]).replace(b"\r\n", b"\n")
        del self.buf[:end + tlen]
        lines = block.split(b"\n")
        self._request_line(lines[0])
        headers = {}
        for line in lines[1:]:
            if not line:
                continue
            if line[0:1] in (b' ', b'\t'):
                raise ParseError(BAD_REQUEST)
            name, sep, value = line.partition(b':')
            if not sep or not name.strip():
                raise ParseError(BAD_REQUEST)
            name = name.strip()
            if b'_' in name:
                # CVE-2015-0219
                continue
            key = 'HTTP_' + name.upper().replace(b'-', b'_').decode('latin-1')
            value = value.strip().decode('latin-1')
            if key in headers:
                headers[key] += ',' + value
            else:
                headers[key] = value
        self.environ = headers
        self._check_body_headers()
        if self.state == 'headers':
            # No body; message complete.
            return self._message_complete()
        if self.expect_continue and not self.buf:
            self.expect_continue = False
            return ('expect_continue',)
        self.expect_continue = False
        # Fall through to body parsing on the next feed() iteration.
        return _AGAIN

    def _request_line(self, line):
        parts = line.split(b' ')
        if len(parts) != 3:
            raise ParseError(BAD_REQUEST)
        method, target, version = parts
        if not method or any(c not in _TOKEN for c in method) \
                or method.upper() != method:
            raise ParseError(BAD_REQUEST)
        if version == b'HTTP/1.1':
            self.http_minor = 1
        elif version == b'HTTP/1.0':
            self.http_minor = 0
        else:
            raise ParseError(BAD_REQUEST)
        self.environ_method = method.decode('latin-1')

        target = target.split(b'#', 1)[0]
        if target.lower().startswith(b'http://'):
            # absolute-form: skip scheme and authority
            slash = target.find(b'/', 7)
            target = target[slash:] if slash != -1 else b'/'
        path, _, query = target.partition(b'?')
        self.environ_path = _unquote(path).decode('latin-1')
        self.environ_query = query.decode('latin-1')

    def _check_body_headers(self):
        headers = self.environ
        te = headers.get('HTTP_TRANSFER_ENCODING')
        cl = headers.get('HTTP_CONTENT_LENGTH')
        if te is not None and cl is not None:
            raise ParseError(BAD_REQUEST)
        if te is not None:
            if 'chunked' not in te.lower():
                raise ParseError(LENGTH_REQUIRED)
            self.state = 'chunked'
            self.body_parts = []
        elif cl is not None:
            if not cl.isascii() or not cl.isdigit():
                raise ParseError(BAD_REQUEST)
            self.body_remaining = int(cl)
            if self.body_remaining:
                self.state = 'content-length'
                self.body_parts = []
        expect = headers.get('HTTP_EXPECT')
        if expect is not None and self.http_minor == 1:
            if expect.lower() == '100-continue':
                self.expect_continue = True
            else:
                raise ParseError(EXPECTATION_FAILED)

    def _parse_content_length(self):
        if len(self.buf) < self.body_remaining:
            return None
        self.body_parts.append(bytes(self.buf[:self.body_remaining]))
        del self.buf[:self.body_remaining]
        self.environ['wsgi.input'] = io.BytesIO(b''.join(self.body_parts))
        return self._message_complete()

    def _parse_chunked(self):
        parts = self.body_parts
        while True:
            nl = self.buf.find(b'\n')
            if nl == -1:
                if len(self.buf) > MAX_HEADER_SIZE:
                    raise ParseError(BAD_REQUEST)
                return None
            line = bytes(self.buf[:nl]).rstrip(b'\r').split(b';', 1)[0]
            if not line or len(line) > 16 \
                    or any(c not in _HEX for c in line):
                raise ParseError(BAD_REQUEST)
            size = int(line, 16)
            if size == 0:
                # Consume trailers up to a blank line.
                found = _header_end(self.buf)
                if found is None:
                    if len(self.buf) > MAX_HEADER_SIZE:
                        raise ParseError(BAD_REQUEST)
                    return None
                del self.buf[:found[0] + found[1]]
                self.environ['wsgi.input'] = io.BytesIO(b''.join(parts))
                return self._message_complete()
            need = nl + 1 + size + 2
            if len(self.buf) < need:
                return None
            parts.append(bytes(self.buf[nl + 1:nl + 1 + size]))
            del self.buf[:need]

    def _message_complete(self):
        env = self.environ
        env['PATH_INFO'] = self.environ_path
        env['QUERY_STRING'] = self.environ_query
        if 'HTTP_CONTENT_LENGTH' in env:
            env['CONTENT_LENGTH'] = env.pop('HTTP_CONTENT_LENGTH')
        if 'HTTP_CONTENT_TYPE' in env:
            env['CONTENT_TYPE'] = env.pop('HTTP_CONTENT_TYPE')
        env['SERVER_PROTOCOL'] = 'HTTP/1.1' if self.http_minor == 1 else 'HTTP/1.0'
        env['REQUEST_METHOD'] = self.environ_method
        env['REMOTE_ADDR'] = self.remote_addr
        env.setdefault('wsgi.input', io.BytesIO())
        self.keep_alive = self._should_keep_alive(env)
        return ('request', env, self.keep_alive, self.http_minor)

    def _should_keep_alive(self, env):
        conn = env.get('HTTP_CONNECTION', '')
        tokens = {t.strip().lower() for t in conn.split(',')}
        if self.http_minor == 1:
            return 'close' not in tokens
        return 'keep-alive' in tokens
