"""Thumbnail decoding, shared by the desktop and web front ends.

Design, in order of what matters for a large folder:

* **Visible first.** Requests carry a priority and a viewport generation.
  Workers drain a priority queue so what is on screen decodes before anything
  queued for prefetch, and work for tiles the user already scrolled past is
  dropped instead of decoded.
* **Decode once, resize many.** Each image is decoded once into a *master*
  thumbnail (512px wide; 1024px for very wide columns). Column-width thumbs
  are produced from the master, so resizing the window or changing the column
  count never re-reads the file.
* **Disk cache.** Masters are written to the user cache directory keyed by
  path, size and modification time, so a folder opened a second time is
  instant, the way Explorer or a Linux file manager behaves.
* **JPEG draft mode.** Large JPEGs are decoded at 1/2, 1/4 or 1/8 scale by the
  codec itself, which is several times faster than a full decode.
* **Orientation.** EXIF rotation is honoured everywhere (thumbs, aspect ratios
  and the full image), so phone photos are not shown sideways.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sys
import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from PIL import Image, ImageFile, ImageOps

Image.MAX_IMAGE_PIXELS = None  # the user chose the folder; do not refuse large scans
ImageFile.LOAD_TRUNCATED_IMAGES = True  # show a slightly damaged photo rather than a broken tile

DEFAULT_ASPECT = 4 / 3   # layout guess for an image whose ratio is not known yet
MASTER_SMALL = 512
MASTER_LARGE = 1024
MAX_MASTER_HEIGHT = 4096

_RESAMPLE_DOWN = Image.Resampling.HAMMING
_RESAMPLE_FINE = Image.Resampling.LANCZOS
_RESAMPLE_UP = Image.Resampling.BICUBIC


# --------------------------------------------------------------------------- utils

def default_cache_dir() -> str:
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'ImageFlow', 'thumbs')
    if sys.platform == 'darwin':
        return os.path.join(os.path.expanduser('~/Library/Caches'), 'ImageFlow', 'thumbs')
    base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    return os.path.join(base, 'imageflow', 'thumbs')


def master_width_for(width: int) -> int:
    return MASTER_SMALL if width <= MASTER_SMALL else MASTER_LARGE


def has_alpha(im: Image.Image) -> bool:
    return im.mode in ('RGBA', 'LA', 'PA') or (im.mode == 'P' and 'transparency' in im.info)


def normalize_mode(im: Image.Image) -> Image.Image:
    """Return an RGB or RGBA image suitable for both Tk and JPEG/PNG encoding."""
    if im.mode in ('RGB', 'RGBA'):
        return im
    if has_alpha(im):
        return im.convert('RGBA')
    if im.mode in ('I', 'I;16', 'I;16B', 'I;16L', 'F'):
        # 16-bit / float data: scale to 8-bit instead of clipping to white.
        try:
            lo, hi = im.getextrema()
            if hi > lo:
                scale = 255.0 / (hi - lo)
                im = im.point(lambda v, lo=lo, scale=scale: (v - lo) * scale)
            return im.convert('L').convert('RGB')
        except Exception:
            pass
    return im.convert('RGB')


def exif_transpose(im: Image.Image) -> Image.Image:
    try:
        out = ImageOps.exif_transpose(im)
        return out if out is not None else im
    except Exception:
        return im


def read_aspect(path: str) -> float:
    """Width / height of an image honouring EXIF orientation, header read only."""
    with Image.open(path) as im:
        w, h = im.size
        try:
            orient = im.getexif().get(0x0112, 1)
        except Exception:
            orient = 1
    if orient in (5, 6, 7, 8):
        w, h = h, w
    return (w / h) if h else 1.0


def open_scaled(path: str, min_w: int, min_h: int) -> Image.Image:
    """Decode ``path`` at least ``min_w`` x ``min_h`` large (JPEG draft shortcut)."""
    with Image.open(path) as im:
        try:
            im.draft('RGB', (max(1, min_w), max(1, min_h)))
        except Exception:
            pass
        im.load()
        out = exif_transpose(im)
    return normalize_mode(out)


def open_full(path: str) -> Image.Image:
    with Image.open(path) as im:
        im.load()
        out = exif_transpose(im)
    return normalize_mode(out)


def resize_to_width(im: Image.Image, width: int) -> Image.Image:
    """Resize keeping aspect so the result is exactly ``width`` wide."""
    w, h = im.size
    if w == width:
        return im
    height = max(1, round(h * width / w))
    if width < w:
        rs = _RESAMPLE_FINE if w <= 2 * width else _RESAMPLE_DOWN
        return im.resize((width, height), rs, reducing_gap=2.0)
    return im.resize((width, height), _RESAMPLE_UP)


# ------------------------------------------------------------------------- caches

class LRUBytes:
    """Thread-safe LRU of images (or wrappers with ``.im``) bounded by memory use."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._d: 'OrderedDict[object, Image.Image]' = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def _size(obj) -> int:
        # Accepts a PIL image or any wrapper exposing one as ``.im``.
        im = obj if isinstance(obj, Image.Image) else obj.im
        return im.width * im.height * len(im.getbands())

    def get(self, key):
        with self._lock:
            im = self._d.get(key)
            if im is not None:
                self._d.move_to_end(key)
            return im

    def put(self, key, im: Image.Image) -> None:
        sz = self._size(im)
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self._bytes -= self._size(old)
            self._d[key] = im
            self._bytes += sz
            while self._bytes > self.max_bytes and len(self._d) > 1:
                _, ev = self._d.popitem(last=False)
                self._bytes -= self._size(ev)

    def __contains__(self, key) -> bool:
        with self._lock:
            return key in self._d

    def __len__(self) -> int:
        return len(self._d)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self._bytes = 0


class DiskCache:
    """Master thumbnails on disk, keyed by path + size + mtime."""

    def __init__(self, directory: str, max_bytes: int = 1024 * 1024 * 1024):
        self.dir = directory
        self.max_bytes = max_bytes
        self.enabled = True
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            self.enabled = False

    @staticmethod
    def key(path: str, mtime_ns: int, size: int, master_w: int) -> str:
        raw = f"{os.path.abspath(path)}|{mtime_ns}|{size}|{master_w}".encode('utf-8', 'surrogateescape')
        return hashlib.sha1(raw).hexdigest()

    def _paths(self, key: str) -> Tuple[str, str]:
        return (os.path.join(self.dir, key + '.jpg'), os.path.join(self.dir, key + '.png'))

    def load(self, key: str) -> Optional[Image.Image]:
        if not self.enabled:
            return None
        for p in self._paths(key):
            if os.path.exists(p):
                try:
                    with Image.open(p) as im:
                        im.load()
                        return im.copy() if im.mode in ('RGB', 'RGBA') else normalize_mode(im)
                except Exception:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        return None

    def store(self, key: str, im: Image.Image) -> None:
        if not self.enabled:
            return
        jpg, png = self._paths(key)
        tmp = os.path.join(self.dir, f".{key}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            if im.mode == 'RGBA':
                im.save(tmp, 'PNG', compress_level=1)
                os.replace(tmp, png)
            else:
                im.save(tmp, 'JPEG', quality=86)
                os.replace(tmp, jpg)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def trim(self) -> None:
        """Delete the oldest files until the cache is under its size cap."""
        if not self.enabled:
            return
        try:
            files = []
            total = 0
            with os.scandir(self.dir) as it:
                for e in it:
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    files.append((st.st_mtime, st.st_size, e.path))
                    total += st.st_size
            if total <= self.max_bytes:
                return
            files.sort()
            target = int(self.max_bytes * 0.8)
            for _, sz, p in files:
                if total <= target:
                    break
                try:
                    os.remove(p)
                    total -= sz
                except OSError:
                    pass
        except OSError:
            pass


# ----------------------------------------------------------------------- service

_STOP = object()


class ThumbnailService:
    """Background thumbnail producer with visible-first scheduling."""

    def __init__(self, workers: Optional[int] = None, disk_cache: bool = True,
                 cache_dir: Optional[str] = None,
                 master_budget: int = 192 * 1024 * 1024,
                 thumb_budget: int = 96 * 1024 * 1024):
        cpu = os.cpu_count() or 4
        # Thumbnails are IO + codec bound; beyond four threads the disk, not the
        # CPU, is the limit, and the machine stays usable while a folder loads.
        self.n_workers = workers or max(2, min(4, cpu - 1))
        self.masters = LRUBytes(master_budget)
        self.thumbs = LRUBytes(thumb_budget)
        self.aspects: Dict[str, float] = {}
        self.failed: set = set()
        self.disk: Optional[DiskCache] = DiskCache(cache_dir or default_cache_dir()) if disk_cache else None

        self._q: 'queue.PriorityQueue' = queue.PriorityQueue()
        self._pending: set = set()
        self._pending_lock = threading.Lock()
        self._seq = 0
        self.results: 'queue.Queue' = queue.Queue()
        self.wanted: frozenset = frozenset()
        self.generation = 0
        self._stopped = False
        self._threads: List[threading.Thread] = []
        for i in range(self.n_workers):
            t = threading.Thread(target=self._worker, name=f"thumb-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        self._aspect_threads: List[threading.Thread] = []
        self._aspect_stop = threading.Event()
        self._aspect_lock = threading.Lock()
        self._aspect_todo: Optional[deque] = None
        self._aspect_total = 0
        self._aspect_done = 0
        self._aspect_progress = None
        self._aspect_report_every = 120
        if self.disk is not None:
            threading.Thread(target=self.disk.trim, name="thumb-trim", daemon=True).start()

    # ------------------------------------------------------------- lifecycle

    def stop(self) -> None:
        self._stopped = True
        self._aspect_stop.set()
        for _ in self._threads:
            self._q.put((-1, 0, 0, _STOP, 0, 0))

    def cancel_all(self) -> None:
        """Drop queued work (folder changed)."""
        self.generation += 1
        self.wanted = frozenset()
        with self._pending_lock:
            self._pending.clear()
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        with self._aspect_lock:
            self._aspect_stop.set()
            self._aspect_todo = None
            self._aspect_progress = None

    def clear_memory(self) -> None:
        self.masters.clear()
        self.thumbs.clear()
        self.aspects.clear()
        self.failed.clear()

    # --------------------------------------------------------------- aspects
    #
    # Aspect ratios drive the masonry layout. Reading one means opening the
    # file header, which on a slow disk (or under antivirus on Windows) costs
    # 5-30 ms per file, so nothing waits for them: the grid paints at once,
    # the ratios near the viewport are read first and everything learnt is
    # persisted per folder so the next open needs no reads at all.

    def aspect(self, path: str) -> Optional[float]:
        return self.aspects.get(path)

    @staticmethod
    def _index_key(name: str, mtime_ns: int, size: int) -> str:
        return f"{name}|{mtime_ns}|{size}"

    def _index_path(self, folder: str) -> Optional[str]:
        if self.disk is None or not self.disk.enabled:
            return None
        d = os.path.join(os.path.dirname(self.disk.dir), 'aspects')
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return None
        h = hashlib.sha1(os.path.abspath(folder).encode('utf-8', 'surrogateescape')).hexdigest()
        return os.path.join(d, h + '.json')

    def load_aspect_index(self, folder: str, entries) -> int:
        """Fill ``aspects`` from the saved index of ``folder``. ``entries`` are
        objects with ``path``, ``name``, ``mtime_ns`` and ``size``. Returns
        how many were restored."""
        p = self._index_path(folder)
        if p is None or not os.path.exists(p):
            return 0
        try:
            with open(p, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return 0
        n = 0
        for e in entries:
            a = data.get(self._index_key(e.name, e.mtime_ns, e.size))
            if isinstance(a, (int, float)) and a > 0:
                self.aspects[e.path] = float(a)
                n += 1
        return n

    def save_aspect_index(self, folder: str, entries) -> None:
        p = self._index_path(folder)
        if p is None:
            return
        data = {}
        for e in entries:
            a = self.aspects.get(e.path)
            if a is not None:
                data[self._index_key(e.name, e.mtime_ns, e.size)] = round(a, 6)
        if not data:
            return
        tmp = f"{p}.{os.getpid()}.tmp"
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, separators=(',', ':'))
            os.replace(tmp, p)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _read_one_aspect(self, p: str) -> None:
        if p in self.aspects:
            return
        try:
            self.aspects[p] = read_aspect(p)
        except Exception:
            self.aspects[p] = 1.0

    def ensure_aspects(self, paths: Iterable[str], on_progress: Optional[Callable[[int, int], None]] = None,
                       threads: int = 3, report_every: int = 120) -> None:
        """Read aspects for ``paths`` in the background, in list order, unless
        ``prioritize_aspects`` moves some to the front. ``on_progress(done,
        total)`` is called from a worker thread periodically and at the end.
        """
        todo = [p for p in paths if p not in self.aspects]
        with self._aspect_lock:
            self._aspect_todo = deque(todo)
            self._aspect_total = len(todo)
            self._aspect_done = 0
            self._aspect_progress = on_progress
            self._aspect_report_every = report_every
            if not todo:
                if on_progress:
                    try:
                        on_progress(0, 0)
                    except Exception:
                        pass
                return
            stop = self._aspect_stop = threading.Event()
            running = sum(1 for t in self._aspect_threads if t.is_alive())
        for i in range(max(0, min(threads, len(todo)) - running)):
            t = threading.Thread(target=self._aspect_worker, args=(stop,), name=f"aspect-{i}", daemon=True)
            t.start()
            self._aspect_threads.append(t)

    def prioritize_aspects(self, paths: Iterable[str]) -> None:
        """Move ``paths`` (visible tiles) to the front of the aspect queue."""
        with self._aspect_lock:
            todo = self._aspect_todo
            if todo is None:
                return
            front = [p for p in paths if p not in self.aspects]
            if not front:
                return
            for p in reversed(front):
                todo.appendleft(p)

    def _aspect_worker(self, stop: threading.Event) -> None:
        while not stop.is_set():
            with self._aspect_lock:
                todo = self._aspect_todo
                if not todo:
                    break
                p = todo.popleft()
            # Skips paths already learnt from a thumbnail or a prioritised read.
            self._read_one_aspect(p)
            with self._aspect_lock:
                self._aspect_done += 1
                d, total = self._aspect_done, self._aspect_total
                cb, every = self._aspect_progress, self._aspect_report_every
                finished = not self._aspect_todo
            if cb and (d % every == 0 or finished):
                try:
                    cb(d, total)
                except Exception:
                    pass
            if finished:
                break

    def read_aspects_now(self, paths: Iterable[str], threads: int = 8, budget: Optional[float] = None) -> None:
        """Blocking parallel header read, for the first screenful. With a
        ``budget`` in seconds the call returns when time is up; whatever is
        left streams in later."""
        todo = [p for p in paths if p not in self.aspects]
        if not todo:
            return
        cursor = [0]
        lock = threading.Lock()
        deadline = (time.monotonic() + budget) if budget is not None else None

        def run():
            while True:
                if deadline is not None and time.monotonic() > deadline:
                    return
                with lock:
                    i = cursor[0]
                    cursor[0] += 1
                if i >= len(todo):
                    return
                self._read_one_aspect(todo[i])

        ts = [threading.Thread(target=run, daemon=True) for _ in range(max(1, min(threads, len(todo))))]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=(budget + 0.5) if budget is not None else None)

    # ------------------------------------------------------------- requests

    def begin_viewport(self, wanted_keys: Iterable[Tuple[str, int]]) -> None:
        """Declare the set of (path, width) currently on screen."""
        self.wanted = frozenset(wanted_keys)
        self.generation += 1

    def get(self, path: str, width: int) -> Optional[Image.Image]:
        return self.thumbs.get((path, width))

    def request(self, path: str, width: int, priority: int = 0) -> bool:
        """Queue a thumbnail. Lower priority value decodes sooner. Returns
        False when the image is already cached, pending or known broken."""
        key = (path, width)
        if path in self.failed or key in self.thumbs:
            return False
        with self._pending_lock:
            if key in self._pending:
                return False
            self._pending.add(key)
            self._seq += 1
            seq = self._seq
        self._q.put((priority, -self.generation, seq, path, width, self.generation))
        return True

    def pending_count(self) -> int:
        with self._pending_lock:
            return len(self._pending)

    def drain(self, limit: int = 64) -> List[Tuple[str, int, Optional[Image.Image]]]:
        out = []
        try:
            while len(out) < limit:
                out.append(self.results.get_nowait())
        except queue.Empty:
            pass
        return out

    # -------------------------------------------------------------- blocking

    def get_sync(self, path: str, width: int) -> Optional[Image.Image]:
        """Produce (or fetch) a thumbnail on the calling thread. Used by the
        web server, whose request handlers are already threads."""
        im = self.thumbs.get((path, width))
        if im is not None:
            return im
        if path in self.failed:
            return None
        im = self._produce(path, width)
        if im is None:
            self.failed.add(path)
        return im

    def get_master_sync(self, path: str, master_w: int = MASTER_SMALL) -> Optional[Image.Image]:
        if path in self.failed:
            return None
        return self._master(path, master_w)

    # ---------------------------------------------------------------- worker

    def _worker(self) -> None:
        while not self._stopped:
            try:
                prio, _, _, path, width, gen = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if path is _STOP:
                break
            key = (path, width)
            # Work from an older viewport that is no longer on screen is
            # stale: dropping it lets the current screen decode first. A tile
            # that comes back into view is simply requested again.
            if gen < self.generation and key not in self.wanted:
                with self._pending_lock:
                    self._pending.discard(key)
                continue
            try:
                im = self.thumbs.get(key)
                if im is None:
                    im = self._produce(path, width)
            except Exception:
                im = None
            with self._pending_lock:
                self._pending.discard(key)
            if im is None:
                self.failed.add(path)
            self.results.put((path, width, im))

    def _produce(self, path: str, width: int) -> Optional[Image.Image]:
        master = self._master(path, master_width_for(width))
        if master is None:
            return None
        thumb = resize_to_width(master, width)
        self.thumbs.put((path, width), thumb)
        return thumb

    def _master(self, path: str, master_w: int) -> Optional[Image.Image]:
        mkey = (path, master_w)
        master = self.masters.get(mkey)
        if master is not None:
            return master
        try:
            st = os.stat(path)
        except OSError:
            return None
        dkey = None
        if self.disk is not None:
            dkey = DiskCache.key(path, st.st_mtime_ns, st.st_size, master_w)
            master = self.disk.load(dkey)
        if master is None:
            try:
                master = self._decode_master(path, master_w)
            except Exception:
                return None
            if self.disk is not None and dkey is not None:
                self.disk.store(dkey, master)
        if master.height:
            self.aspects.setdefault(path, master.width / master.height)
        self.masters.put(mkey, master)
        return master

    @staticmethod
    def _decode_master(path: str, master_w: int) -> Image.Image:
        with Image.open(path) as im:
            try:
                # Ask the JPEG codec for a reduced-size decode. For other
                # formats this is a no-op.
                im.draft('RGB', (master_w, master_w))
            except Exception:
                pass
            im.load()
            out = exif_transpose(im)
        out = normalize_mode(out)
        w, h = out.size
        tw = min(master_w, w)
        th = max(1, round(h * tw / w)) if w else 1
        if th > MAX_MASTER_HEIGHT:
            th = MAX_MASTER_HEIGHT
            tw = max(1, round(w * th / h))
        if (tw, th) != (w, h):
            rs = _RESAMPLE_FINE if w <= 2 * tw else _RESAMPLE_DOWN
            out = out.resize((tw, th), rs, reducing_gap=2.0)
        return out
