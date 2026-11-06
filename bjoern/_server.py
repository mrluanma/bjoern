import errno
import os
import selectors
import signal
import socket
import threading
import traceback

from . import _parser, _wsgi

READ_BUFFER_SIZE = 64 * 1024
SENDFILE_CHUNK = 1024 * 1024
MAX_ACCEPTS = 64

_SENDFILE_FALLBACK_ERRNOS = {errno.EINVAL, errno.ENOTSOCK, errno.ENOSYS}
for _name in ('EOPNOTSUPP', 'ENOTSUP'):
    if hasattr(errno, _name):
        _SENDFILE_FALLBACK_ERRNOS.add(getattr(errno, _name))


class _Abort(Exception):
    pass


class Connection:
    def __init__(self, sock, addr):
        self.sock = sock
        self.fd = sock.fileno()
        remote_addr = str(addr[0]) if isinstance(addr, tuple) else ''
        self.parser = _parser.RequestParser(remote_addr)
        self.events = []
        self.state = 'read'  # 'read' | 'write' | 'closed'
        self.response = None
        self.out = None
        self.out_pos = 0
        self.sendfile_offset = 0
        self.sendfile_ok = False


def server_run(sock, wsgi_app):
    own_sock = None
    if isinstance(sock, int):
        own_sock = sock = socket.socket(fileno=os.dup(sock))
    elif not isinstance(sock, socket.socket):
        own_sock = sock = socket.socket(fileno=os.dup(sock.fileno()))
    sock.setblocking(False)

    environ = _wsgi.base_environ(sock)
    sel = selectors.DefaultSelector()
    connections = {}
    stopping = [False]

    wakeup_r = wakeup_w = None
    old_sigint = None
    if threading.current_thread() is threading.main_thread():
        wakeup_r, wakeup_w = socket.socketpair()
        wakeup_r.setblocking(False)
        wakeup_w.setblocking(False)

        def on_sigint(signum, frame):
            stopping[0] = True
            try:
                wakeup_w.send(b'\0')
            except OSError:
                pass

        old_sigint = signal.signal(signal.SIGINT, on_sigint)
        sel.register(wakeup_r, selectors.EVENT_READ, 'wakeup')

    sel.register(sock, selectors.EVENT_READ, 'listener')

    def close_conn(conn):
        if conn.state == 'closed':
            return
        conn.state = 'closed'
        try:
            sel.unregister(conn.sock)
        except Exception:
            pass
        try:
            conn.sock.close()
        except OSError:
            pass
        if conn.response is not None:
            conn.response.close_iterable()
        connections.pop(conn.fd, None)

    def send_error_and_close(conn, code):
        conn.out = _parser.ERROR_MESSAGES[code]
        conn.out_pos = 0
        conn.response = None
        conn.state = 'write'
        try:
            done = try_send(conn)
        except _Abort:
            close_conn(conn)
            return
        if done:
            close_conn(conn)
        else:
            modify(conn, selectors.EVENT_WRITE)

    def modify(conn, events):
        try:
            sel.modify(conn.sock, events, conn)
        except Exception:
            close_conn(conn)

    def accept_all():
        for _ in range(MAX_ACCEPTS):
            try:
                client, addr = sock.accept()
            except (BlockingIOError, OSError):
                return
            client.setblocking(False)
            conn = Connection(client, addr)
            connections[conn.fd] = conn
            sel.register(client, selectors.EVENT_READ, conn)

    def process_events(conn):
        while conn.events and conn.state == 'read':
            ev = conn.events.pop(0)
            if ev[0] == 'expect_continue':
                try:
                    conn.sock.sendall(_parser.CONTINUE)
                except (BlockingIOError, OSError):
                    close_conn(conn)
                    return
            else:
                _, env, keep_alive, minor = ev
                env.update(environ)
                start_response(conn, env, keep_alive, minor)

    def start_response(conn, env, req_keep_alive, minor):
        try:
            resp = _wsgi.call_application(wsgi_app, env, req_keep_alive, minor)
        except Exception:
            traceback.print_exc()
            send_error_and_close(conn, _parser.SERVER_ERROR)
            return
        conn.response = resp
        conn.out = resp.buffer
        conn.out_pos = 0
        conn.state = 'write'
        if not pump_write(conn):
            modify(conn, selectors.EVENT_WRITE)

    def try_send(conn):
        """Send as much of conn.out as possible; True if fully sent."""
        while conn.out_pos < len(conn.out):
            try:
                n = conn.sock.send(memoryview(conn.out)[conn.out_pos:])
            except BlockingIOError:
                return False
            except OSError:
                raise _Abort
            if n == 0:
                raise _Abort
            conn.out_pos += n
        return True

    def pump_write(conn):
        """Advance the write state machine.

        Returns False when the socket would block and the caller must wait
        for writability; True otherwise (response progressing/done/aborted).
        """
        resp = conn.response
        try:
            while True:
                if conn.out is not None:
                    if not try_send(conn):
                        return False
                    conn.out = None
                    conn.out_pos = 0
                if resp is not None and resp.sendfile_fd != -1:
                    if not do_sendfile(conn, resp):
                        return False
                    continue
                if resp is None:
                    # Error response: single buffer, then close.
                    close_conn(conn)
                    return True
                try:
                    nxt = resp.next_out()
                except Exception:
                    traceback.print_exc()
                    raise _Abort
                if nxt is _wsgi.FINISHED:
                    response_done(conn)
                    return True
                conn.out = nxt
                conn.out_pos = 0
        except _Abort:
            close_conn(conn)
            return True

    def do_sendfile(conn, resp):
        """True to keep pumping (finished or fell back); False if blocked."""
        fd = resp.sendfile_fd
        while True:
            try:
                if not conn.sendfile_ok:
                    conn.sendfile_offset = os.lseek(fd, 0, os.SEEK_CUR)
                n = os.sendfile(conn.sock.fileno(), fd,
                                conn.sendfile_offset, SENDFILE_CHUNK)
            except BlockingIOError:
                return False
            except OSError as e:
                if e.errno in _SENDFILE_FALLBACK_ERRNOS and not conn.sendfile_ok:
                    # sendfile unsupported: fall back to plain iteration
                    # (non-chunked: Content-Length is known on this path).
                    resp.sendfile_fd = -1
                    resp.iterator = iter(resp.iterable)
                    return True
                raise _Abort
            conn.sendfile_ok = True
            if n == 0:
                resp.sendfile_fd = -1
                resp.iterator = None
                return True
            conn.sendfile_offset += n

    def response_done(conn):
        resp = conn.response
        keep = resp.keep_alive if resp is not None else False
        if resp is not None:
            resp.close_iterable()
        conn.response = None
        conn.out = None
        conn.out_pos = 0
        conn.sendfile_ok = False
        conn.sendfile_offset = 0
        if stopping[0] or not keep:
            close_conn(conn)
            return
        conn.state = 'read'
        modify(conn, selectors.EVENT_READ)
        if conn.state == 'closed':
            return
        try:
            conn.events += conn.parser.feed(b'')
        except _parser.ParseError as e:
            send_error_and_close(conn, e.code)
            return
        process_events(conn)

    def on_read(conn):
        try:
            data = conn.sock.recv(READ_BUFFER_SIZE)
        except BlockingIOError:
            return
        except (ConnectionError, OSError):
            close_conn(conn)
            return
        if not data:
            close_conn(conn)
            return
        try:
            conn.events += conn.parser.feed(data)
        except _parser.ParseError as e:
            send_error_and_close(conn, e.code)
            return
        process_events(conn)

    try:
        while True:
            for key, mask in sel.select():
                data = key.data
                if data == 'listener':
                    accept_all()
                elif data == 'wakeup':
                    try:
                        wakeup_r.recv(4096)
                    except OSError:
                        pass
                else:
                    conn = data
                    try:
                        if conn.state == 'read':
                            on_read(conn)
                        elif conn.state == 'write':
                            pump_write(conn)
                    except Exception:
                        traceback.print_exc()
                        close_conn(conn)
            if stopping[0]:
                try:
                    sel.unregister(sock)
                except Exception:
                    pass
                for conn in list(connections.values()):
                    if conn.state == 'read' and not conn.parser.buf \
                            and not conn.events:
                        close_conn(conn)
                if not connections:
                    raise KeyboardInterrupt
    finally:
        for conn in list(connections.values()):
            close_conn(conn)
        try:
            sel.unregister(sock)
        except Exception:
            pass
        sel.close()
        if wakeup_r is not None:
            wakeup_r.close()
            wakeup_w.close()
        if old_sigint is not None:
            signal.signal(signal.SIGINT, old_sigint)
        if own_sock is not None:
            own_sock.close()
