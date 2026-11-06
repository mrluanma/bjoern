import io
import os
import sys
import traceback

from . import _parser


class FileWrapper:
    def __init__(self, file, blocksize=None):
        self.file = file
        self.blocksize = blocksize
        try:
            self.fd = file.fileno()
        except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
            self.fd = -1

    def __iter__(self):
        return self

    def __next__(self):
        if self.blocksize:
            data = self.file.read(self.blocksize)
        else:
            data = self.file.read()
        if not data:
            raise StopIteration
        return data

    def close(self):
        if hasattr(self.file, 'close'):
            self.file.close()


def base_environ(sock):
    try:
        sockname = sock.getsockname()
    except OSError:
        sockname = None
    if isinstance(sockname, tuple) and len(sockname) >= 2:
        server_name, server_port = str(sockname[0]), str(sockname[1])
    else:
        server_name = server_port = ''
    return {
        'wsgi.file_wrapper': FileWrapper,
        'SCRIPT_NAME': '',
        'wsgi.version': (1, 0),
        'wsgi.input_terminated': True,
        'wsgi.url_scheme': 'http',
        'wsgi.errors': sys.stderr,
        'wsgi.multithread': False,
        'wsgi.multiprocess': True,
        'wsgi.run_once': False,
        'SERVER_NAME': server_name,
        'SERVER_PORT': server_port,
    }


class StartResponse:
    def __init__(self, response):
        self.response = response

    def __call__(self, status, headers, exc_info=None):
        resp = self.response
        if resp.start_response_called:
            resp.status = None
            resp.headers = None
            resp.length_unknown = True

        if exc_info is not None:
            if not isinstance(exc_info, tuple) or len(exc_info) != 3:
                raise TypeError(
                    "start_response argument 3 must be a 3-tuple (got "
                    "'%.200s')" % type(exc_info).__name__)
            if resp.headers_sent:
                # Too late to change headers; let the exception propagate.
                raise exc_info[1].with_traceback(exc_info[2])
            traceback.print_exception(exc_info[0], exc_info[1], exc_info[2])
        elif resp.start_response_called:
            raise TypeError("'start_response' called twice without passing "
                            "'exc_info' the second time")

        if not isinstance(status, str):
            raise TypeError("start_response argument 1 must be a str (got "
                            "'%.200s')" % type(status).__name__)
        status_bytes = status.encode('latin-1')
        if len(status_bytes) < 3:
            raise ValueError("'status' must be 3-digit")

        resp.headers = self._clean_headers(headers)
        resp.status = status_bytes
        resp.start_response_called = True
        return None

    def _clean_headers(self, headers):
        resp = self.response
        if not isinstance(headers, list):
            raise TypeError(
                "start_response argument 2 must be a list of 2-tuples (got "
                "'%.200s')" % type(headers).__name__)
        for i, item in enumerate(headers):
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError(
                    "start_response argument 2 must be a list of 2-tuples "
                    "(field: str, value: str) (found invalid '%.200s' object "
                    "at position %d)" % (type(item).__name__, i))
        new_headers = []
        found_content_length = False
        for i, (field, value) in enumerate(headers):
            if not isinstance(field, str) or not isinstance(value, str):
                raise TypeError(
                    "start_response argument 2 must be a list of 2-tuples "
                    "(field: str, value: str) (found invalid ('%.200s', "
                    "'%.200s') tuple at position %d)"
                    % (type(field).__name__, type(value).__name__, i))
            field = field.encode('latin-1')
            value = value.encode('latin-1')
            new_headers.append((field, value))
            if field.lower() == b'content-length':
                found_content_length = True
        resp.length_unknown = not found_content_length
        return new_headers


def wrap_chunk(chunk):
    return b"%x\r\n" % len(chunk) + chunk + b"\r\n"


FINISHED = object()


class Response:
    """The in-flight response for one request.

    `buffer` holds not-yet-sent bytes (headers first, then body chunks /
    chunk terminator). `next_out()` advances to the next chunk.
    """

    def __init__(self):
        self.start_response_called = False
        self.headers_sent = False
        self.status = None
        self.headers = None
        self.length_unknown = True
        self.keep_alive = False
        self.chunked = False
        self.buffer = b''
        self.iterator = None
        self.iterable = None
        self.sendfile_fd = -1
        self._terminator_sent = False
        self._closed = False

    def header_bytes(self):
        buf = b"HTTP/1.1 " + self.status
        for field, value in self.headers:
            buf += b"\r\n" + field + b": " + value
        if self.keep_alive:
            buf += b"\r\nConnection: Keep-Alive"
            if self.chunked:
                buf += b"\r\nTransfer-Encoding: chunked"
        else:
            buf += b"\r\nConnection: close"
        return buf + b"\r\n\r\n"

    def next_out(self):
        """Return the next bytes to send, or FINISHED."""
        if self.iterator is not None:
            chunk = next_chunk(self.iterator)
            if chunk is not None:
                return wrap_chunk(chunk) if self.chunked else chunk
            self.iterator = None
            if self.chunked:
                self.chunked = False
                return b"0\r\n\r\n"
            return FINISHED
        if self.chunked and not self._terminator_sent:
            self._terminator_sent = True
            return b"0\r\n\r\n"
        return FINISHED

    def close_iterable(self):
        if self._closed:
            return
        self._closed = True
        if self.iterable is not None:
            close = getattr(self.iterable, 'close', None)
            if close is not None:
                try:
                    close()
                except Exception:
                    traceback.print_exc()
            self.iterable = None


def next_chunk(iterator):
    """Next non-empty bytes item, or None when exhausted."""
    while True:
        try:
            item = next(iterator)
        except StopIteration:
            return None
        if not isinstance(item, bytes):
            raise TypeError("wsgi iterable items must be bytes (got "
                            "'%.200s')" % type(item).__name__)
        if item:
            return item


def call_application(app, environ, request_keep_alive, http_minor):
    """Run the WSGI app and build a Response. May raise."""
    resp = Response()
    retval = None
    try:
        retval = app(environ, StartResponse(resp))
        _build(resp, retval, request_keep_alive, http_minor)
    except Exception:
        if retval is not None:
            close = getattr(retval, 'close', None)
            if close is not None:
                try:
                    close()
                except Exception:
                    traceback.print_exc()
        raise
    return resp


def _build(resp, retval, request_keep_alive, http_minor):
    first_chunk = None
    if isinstance(retval, list) and len(retval) == 1 \
            and isinstance(retval[0], bytes):
        retval = retval[0]

    if isinstance(retval, bytes):
        if retval:
            first_chunk = retval
    elif (not resp.length_unknown and type(retval) is FileWrapper
            and retval.fd != -1 and hasattr(os, 'sendfile')):
        resp.iterable = retval
        resp.sendfile_fd = retval.fd
    else:
        resp.iterable = retval
        resp.iterator = iter(retval)
        first_chunk = next_chunk(resp.iterator)
        if first_chunk is None:
            resp.iterator = None

    if resp.headers is None:
        raise RuntimeError(
            "wsgi application returned before start_response was called")

    if resp.status[:3] in (b'204', b'304'):
        resp.length_unknown = False

    if request_keep_alive:
        if resp.length_unknown:
            if http_minor == 1:
                resp.chunked = True
                resp.keep_alive = True
            else:
                resp.keep_alive = False
        else:
            resp.keep_alive = True
    else:
        resp.keep_alive = False

    buf = resp.header_bytes()
    resp.headers_sent = True
    if first_chunk is not None:
        if resp.chunked:
            first_chunk = wrap_chunk(first_chunk)
        buf += first_chunk
    resp.buffer = buf
    return resp
