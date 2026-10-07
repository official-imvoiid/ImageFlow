"""Localhost web viewer.

Serves the same Library (and selection) the desktop window uses, so the two
stay in sync, or runs on its own with ``--serve --no-gui``. Standard library
only: ``http.server`` with a thread per request, bound to 127.0.0.1 unless
the user asks otherwise.

Security notes for a localhost service:

* Every request must carry a Host header naming this server, which defeats
  DNS-rebinding pages that try to reach ``localhost`` services.
* State-changing requests (POST) are refused when an Origin header names a
  different site, so a web page the user happens to have open cannot drive
  the viewer.
* Only images inside the opened folder are served, addressed by index, never
  by path.
* The page is view-only by default: it shows the desktop app's selection but
  cannot change it (``--web-edit`` enables selecting from the browser).
  Export and import of selection lists are desktop-only.
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import string
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

from PIL import Image

from . import __version__
from .library import Library
from .thumbs import ThumbnailService, read_aspect, open_scaled

THUMB_WIDTHS = (192, 256, 320, 384, 512, 640, 768)
DEFAULT_PORT = 6001
# Ports Chrome, Edge and Firefox refuse to connect to (ERR_UNSAFE_PORT), so a
# server there would start fine and still be unreachable from a browser.
BROWSER_BLOCKED_PORTS = frozenset({
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79, 87, 95, 101, 102,
    103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137, 139, 143, 161, 179, 389, 427, 465,
    512, 513, 514, 515, 526, 530, 531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993,
    995, 1719, 1720, 1723, 2049, 3659, 4045, 4190, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668,
    6669, 6697, 10080,
})


def browser_safe_port(port: int) -> int:
    """The first port at or above ``port`` that browsers will connect to."""
    while port in BROWSER_BLOCKED_PORTS and port < 65535:
        port += 1
    return port
BROWSER_NATIVE = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')


def bucket_width(w: int) -> int:
    for b in THUMB_WIDTHS:
        if w <= b:
            return b
    return THUMB_WIDTHS[-1]


class _BytesLRU:
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._d: 'OrderedDict[object, bytes]' = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key) -> Optional[bytes]:
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def put(self, key, value: bytes) -> None:
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self._bytes -= len(old)
            self._d[key] = value
            self._bytes += len(value)
            while self._bytes > self.max_bytes and len(self._d) > 1:
                _, ev = self._d.popitem(last=False)
                self._bytes -= len(ev)


class WebApp:
    """Shared state for request handlers."""

    def __init__(self, library: Library, service: ThumbnailService, allow_open: bool = True,
                 allow_edit: bool = False):
        self.lib = library
        self.service = service
        self.allow_open = allow_open
        # The web page is a viewer. Selection changes come from the desktop
        # app unless the server was started with --web-edit.
        self.allow_edit = allow_edit
        self.encoded = _BytesLRU(96 * 1024 * 1024)
        self.started = time.time()
        with open(os.path.join(STATIC_DIR, 'index.html'), 'rb') as f:
            self.index_html = f.read()

    # ---------------------------------------------------------------- views

    def state(self) -> dict:
        return {
            'folder': self.lib.folder,
            'count': len(self.lib),
            'selected': self.lib.selected_count(),
            'version': self.lib.version,
            'scan_id': self.lib.scan_id,
            'version_app': __version__,
            'allow_open': self.allow_open,
            'read_only': not self.allow_edit,
        }

    def selection(self) -> dict:
        """Just the selected indices: what the poll needs, not the whole list."""
        st = self.state()
        with self.lib.lock:
            st['selected_ids'] = [e.index for e in self.lib.entries if e.selected]
        return st

    def images(self) -> dict:
        aspects = self.service.aspects
        with self.lib.lock:
            items = [[e.index, e.name, 1 if e.selected else 0, aspects.get(e.path)] for e in self.lib.entries]
        st = self.state()
        st['images'] = items
        return st

    def aspects(self, ids) -> dict:
        out = {}
        paths = []
        for i in ids:
            e = self.lib.get(i)
            if e is None:
                continue
            a = self.service.aspects.get(e.path)
            if a is None:
                paths.append(e.path)
            else:
                out[i] = a
        if paths:
            self.service.read_aspects_now(paths, threads=8)
            for i in ids:
                e = self.lib.get(i)
                if e is not None and i not in out:
                    out[i] = self.service.aspects.get(e.path, 1.0)
        return out

    def thumb(self, index: int, width: int) -> Optional[Tuple[bytes, str]]:
        e = self.lib.get(index)
        if e is None:
            return None
        width = bucket_width(width)
        key = (e.path, width, e.mtime_ns, e.size)
        data = self.encoded.get(key)
        if data is not None:
            return data, ('image/png' if data[:4] == b'\x89PNG' else 'image/jpeg')
        im = self.service.get_sync(e.path, width)
        if im is None:
            return None
        buf = io.BytesIO()
        if im.mode == 'RGBA':
            im.save(buf, 'PNG', compress_level=3)
            ctype = 'image/png'
        else:
            im.save(buf, 'JPEG', quality=84)
            ctype = 'image/jpeg'
        data = buf.getvalue()
        self.encoded.put(key, data)
        return data, ctype

    def converted_image(self, index: int, max_px: int = 4096) -> Optional[Tuple[bytes, str]]:
        """Formats browsers cannot show (TIFF) are transcoded on the fly."""
        e = self.lib.get(index)
        if e is None:
            return None
        key = ('full', e.path, e.mtime_ns, e.size, max_px)
        data = self.encoded.get(key)
        if data is not None:
            return data, ('image/png' if data[:4] == b'\x89PNG' else 'image/jpeg')
        try:
            im = open_scaled(e.path, max_px, max_px)
            if im.width > max_px or im.height > max_px:
                im.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
        except Exception:
            return None
        buf = io.BytesIO()
        if im.mode == 'RGBA':
            im.save(buf, 'PNG', compress_level=3)
            ctype = 'image/png'
        else:
            im.save(buf, 'JPEG', quality=90)
            ctype = 'image/jpeg'
        data = buf.getvalue()
        self.encoded.put(key, data)
        return data, ctype

    # ------------------------------------------------------------- mutations

    def select(self, body: dict) -> dict:
        if body.get('clear'):
            self.lib.clear_selection()
        elif 'toggle' in body:
            self.lib.toggle(int(body['toggle']))
        elif 'range' in body:
            a, b = body['range']
            self.lib.select_range(int(a), int(b))
        elif 'ids' in body:
            self.lib.set_selected([int(i) for i in body['ids']], bool(body.get('value', True)))
        return self.state()

    def open_folder(self, folder: str) -> dict:
        folder = os.path.abspath(os.path.expanduser(folder))
        if not os.path.isdir(folder):
            raise FileNotFoundError(folder)
        self.service.cancel_all()
        self.service.clear_memory()
        entries = Library.scan_folder(folder)
        self.service.load_aspect_index(folder, entries)
        self.service.read_aspects_now([e.path for e in entries[:48]], threads=8, budget=0.15)
        self.lib.set_entries(folder, entries)
        rest = [e.path for e in entries if e.path not in self.service.aspects]
        if rest:
            def done(d, total):
                if total and d >= total:
                    self.service.save_aspect_index(folder, entries)
            self.service.ensure_aspects(rest, on_progress=done)
        return self.state()

    @staticmethod
    def browse(path: str) -> dict:
        if not path:
            path = os.path.expanduser('~')
        path = os.path.abspath(os.path.expanduser(path))
        dirs = []
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_dir() and not e.name.startswith('.'):
                            dirs.append(e.name)
                    except OSError:
                        pass
        except OSError as err:
            return {'path': path, 'error': str(err), 'dirs': [], 'parent': os.path.dirname(path), 'drives': _drives()}
        dirs.sort(key=str.lower)
        parent = os.path.dirname(path)
        return {'path': path, 'parent': parent if parent != path else None, 'dirs': dirs, 'drives': _drives(),
                'home': os.path.expanduser('~')}


def _drives():
    if sys.platform != 'win32':
        return ['/']
    out = []
    for letter in string.ascii_uppercase:
        d = f"{letter}:\\"
        if os.path.exists(d):
            out.append(d)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = f"ImageFlow/{__version__}"
    protocol_version = "HTTP/1.1"
    app: WebApp = None  # set per server class
    allowed_hosts: set = set()

    def log_message(self, fmt, *args):  # quiet by default
        if os.environ.get('IMAGEFLOW_HTTP_LOG'):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -------------------------------------------------------------- helpers

    def _send(self, status: int, body: bytes, ctype: str = 'application/json; charset=utf-8',
              extra: Optional[dict] = None):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        self._send(status, json.dumps(obj, separators=(',', ':')).encode('utf-8'))

    def _error(self, status: int, message: str):
        self._json({'error': message}, status)

    def _host_ok(self) -> bool:
        host = (self.headers.get('Host') or '').strip().lower()
        if not host:
            return False
        name = host.rsplit(':', 1)[0] if host.count(':') == 1 else host
        if name.startswith('[') and name.endswith(']'):
            name = name[1:-1]
        return name in self.allowed_hosts or host in self.allowed_hosts

    def _origin_ok(self) -> bool:
        origin = self.headers.get('Origin')
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        return parsed.scheme == 'http' and (parsed.hostname or '').lower() in self.allowed_hosts \
            and (parsed.port or 80) == self.server.server_address[1]

    def _read_json(self) -> dict:
        n = int(self.headers.get('Content-Length') or 0)
        if n > 16 * 1024 * 1024:
            raise ValueError('body too large')
        raw = self.rfile.read(n) if n else b''
        return json.loads(raw.decode('utf-8')) if raw else {}

    # ---------------------------------------------------------------- routes

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, 'bad host')
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query)
        path = url.path
        app = self.app
        try:
            if path in ('/', '/index.html'):
                return self._send(200, app.index_html, 'text/html; charset=utf-8',
                                  {'Cache-Control': 'no-cache'})
            if path == '/api/state':
                return self._json(app.state())
            if path == '/api/images':
                return self._json(app.images())
            if path == '/api/selection':
                return self._json(app.selection())
            if path == '/api/aspects':
                ids = [int(x) for x in (q.get('ids', [''])[0]).split(',') if x.strip().isdigit()]
                return self._json(app.aspects(ids[:1000]))
            if path == '/api/browse':
                if not app.allow_open:
                    return self._error(HTTPStatus.FORBIDDEN, 'folder changes disabled')
                return self._json(app.browse(q.get('path', [''])[0]))
            if path.startswith('/thumb/'):
                return self._thumb(path[len('/thumb/'):], q)
            if path.startswith('/image/'):
                return self._image(path[len('/image/'):], q)
            return self._error(HTTPStatus.NOT_FOUND, 'not found')
        except (ValueError, KeyError) as err:
            return self._error(HTTPStatus.BAD_REQUEST, str(err))
        except (BrokenPipeError, ConnectionResetError):
            return None

    def do_POST(self):
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, 'bad host')
        if not self._origin_ok():
            return self._error(HTTPStatus.FORBIDDEN, 'cross-origin request refused')
        path = urllib.parse.urlsplit(self.path).path
        app = self.app
        try:
            body = self._read_json()
            if path == '/api/select':
                if not app.allow_edit:
                    return self._error(HTTPStatus.FORBIDDEN, 'view only: select in the desktop app')
                return self._json(app.select(body))
            if path == '/api/open':
                if not app.allow_open:
                    return self._error(HTTPStatus.FORBIDDEN, 'folder changes disabled')
                try:
                    return self._json(app.open_folder(str(body.get('folder', ''))))
                except (FileNotFoundError, NotADirectoryError, PermissionError) as err:
                    return self._error(HTTPStatus.BAD_REQUEST, f'cannot open folder: {err}')
            return self._error(HTTPStatus.NOT_FOUND, 'not found')
        except (ValueError, KeyError, TypeError) as err:
            return self._error(HTTPStatus.BAD_REQUEST, str(err))
        except (BrokenPipeError, ConnectionResetError):
            return None

    # --------------------------------------------------------------- images

    def _etag_for(self, e) -> str:
        return f'"{e.mtime_ns:x}-{e.size:x}"'

    def _thumb(self, rest: str, q):
        if not rest.isdigit():
            return self._error(HTTPStatus.BAD_REQUEST, 'bad index')
        idx = int(rest)
        e = self.app.lib.get(idx)
        if e is None:
            return self._error(HTTPStatus.NOT_FOUND, 'no such image')
        width = bucket_width(int(q.get('w', ['320'])[0]))
        etag = f'"{e.mtime_ns:x}-{e.size:x}-{width}"'
        if self.headers.get('If-None-Match') == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header('ETag', etag)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return None
        res = self.app.thumb(idx, width)
        if res is None:
            return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, 'cannot decode image')
        data, ctype = res
        return self._send(200, data, ctype, {'Cache-Control': 'private, max-age=86400', 'ETag': etag})

    def _image(self, rest: str, q):
        if not rest.isdigit():
            return self._error(HTTPStatus.BAD_REQUEST, 'bad index')
        idx = int(rest)
        e = self.app.lib.get(idx)
        if e is None:
            return self._error(HTTPStatus.NOT_FOUND, 'no such image')
        etag = self._etag_for(e)
        if self.headers.get('If-None-Match') == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header('ETag', etag)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return None
        headers = {'Cache-Control': 'private, max-age=86400', 'ETag': etag}
        if e.ext in BROWSER_NATIVE and 'fit' not in q:
            ctype = mimetypes.types_map.get(e.ext) or 'application/octet-stream'
            try:
                size = os.path.getsize(e.path)
                with open(e.path, 'rb') as f:
                    self.send_response(200)
                    self.send_header('Content-Type', ctype)
                    self.send_header('Content-Length', str(size))
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    if self.command == 'HEAD':
                        return None
                    while True:
                        chunk = f.read(256 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return None
            except OSError:
                return self._error(HTTPStatus.NOT_FOUND, 'file unreadable')
        max_px = int(q.get('fit', ['4096'])[0])
        res = self.app.converted_image(idx, max(256, min(max_px, 8192)))
        if res is None:
            return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, 'cannot decode image')
        data, ctype = res
        return self._send(200, data, ctype, headers)


class ImageFlowServer:
    """A running server: ``url``, ``shutdown()``."""

    def __init__(self, httpd: ThreadingHTTPServer, thread: Optional[threading.Thread]):
        self.httpd = httpd
        self.thread = thread
        host, port = httpd.server_address[:2]
        shown = 'localhost' if host in ('127.0.0.1', '0.0.0.0', '::1', '') else host
        self.host, self.port = host, port
        self.requested_port = port
        self.url = f"http://{shown}:{port}/"

    def shutdown(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


def _make_server(library: Library, service: ThumbnailService, host: str, port: int,
                 attempts: int = 20, allow_open: bool = True, allow_edit: bool = False) -> ThreadingHTTPServer:
    app = WebApp(library, service, allow_open=allow_open, allow_edit=allow_edit)
    allowed = {host.lower(), 'localhost', '127.0.0.1', '::1'}
    if host in ('0.0.0.0', ''):
        import socket
        try:
            allowed.add(socket.gethostname().lower())
            allowed.update(ip for ip in socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:
            pass

    class BoundHandler(Handler):
        pass

    BoundHandler.app = app
    BoundHandler.allowed_hosts = allowed

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    last = None
    for p in range(port, port + attempts):
        if p in BROWSER_BLOCKED_PORTS:
            continue
        try:
            httpd = Server((host, p), BoundHandler)
            httpd.allowed_hosts = allowed  # for debugging
            return httpd
        except PermissionError as err:
            raise OSError(f"port {p} is not allowed for this user: {err}")
        except OSError as err:
            last = err
    raise OSError(f"no free port between {port} and {port + attempts - 1}: {last}")


def start_server(library: Library, service: ThumbnailService, host: str = '127.0.0.1',
                 port: int = DEFAULT_PORT, allow_open: bool = True, allow_edit: bool = False) -> ImageFlowServer:
    """Start serving in a daemon thread and return immediately."""
    httpd = _make_server(library, service, host, port, allow_open=allow_open, allow_edit=allow_edit)
    t = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.25},
                         name="imageflow-http", daemon=True)
    t.start()
    return ImageFlowServer(httpd, t)


def serve_forever(library: Library, service: ThumbnailService, host: str = '127.0.0.1',
                  port: int = DEFAULT_PORT, allow_open: bool = True, open_browser: bool = True,
                  allow_edit: bool = False) -> None:
    """Headless mode: block until Ctrl+C."""
    httpd = _make_server(library, service, host, port, allow_open=allow_open, allow_edit=allow_edit)
    srv = ImageFlowServer(httpd, None)
    print(f"ImageFlow web view: {srv.url}  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        try:
            webbrowser.open(srv.url)
        except Exception:
            pass
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        service.stop()
