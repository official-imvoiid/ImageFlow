"""Folder model: what images exist, in what order, and which are selected.

Everything here is independent of Tk so the desktop app and the localhost web
viewer can share one instance. Mutations are guarded by a re-entrant lock and
bump ``version`` so any front end can cheaply detect that something changed.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Callable, Iterable, List, Optional, Sequence

IMAGE_EXTS = frozenset({'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tiff', '.tif'})

# natsort gives the same ordering as Windows Explorer ("1, 2, 10" rather than
# "1, 10, 2"). It is an optional dependency: a missing install degrades to an
# equivalent pure-Python key instead of a crash at import time.
try:  # pragma: no cover - exercised implicitly depending on the environment
    from natsort import natsort_keygen, ns

    _NATKEY = natsort_keygen(alg=ns.IGNORECASE)
except Exception:  # pragma: no cover
    _NUM_RE = re.compile(r'(\d+)')

    def _NATKEY(text: str):
        return [int(tok) if tok.isdigit() else tok.lower() for tok in _NUM_RE.split(text)]


def natural_key(text: str):
    """Sort key giving human ("natural") ordering, case-insensitive."""
    return _NATKEY(text)


class ImageEntry:
    """One image file. ``index`` is its position in the sorted folder."""

    __slots__ = ('path', 'name', 'stem', 'ext', 'index', 'selected', 'mtime_ns', 'size')

    def __init__(self, path: str, name: str, index: int = 0,
                 mtime_ns: int = 0, size: int = 0):
        self.path = path
        self.name = name
        stem, ext = os.path.splitext(name)
        self.stem = stem
        self.ext = ext.lower()
        self.index = index
        self.selected = False
        self.mtime_ns = mtime_ns
        self.size = size

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ImageEntry({self.name!r}, selected={self.selected})"


def _clean_token(token: str) -> str:
    token = token.strip().strip('"').strip("'").strip()
    if not token:
        return ''
    # Accept full paths from either operating system; only the file name matters.
    token = token.replace('\\', '/').rsplit('/', 1)[-1]
    return token.strip()


def parse_name_list(text: str) -> List[str]:
    """Split an exported list back into file names.

    Accepts one name per line, comma or semicolon separated values, quoted
    names, Windows or POSIX paths, and a UTF-8 BOM. Order is preserved and
    duplicates removed.
    """
    if text.startswith('﻿'):
        text = text[1:]
    if ',' in text or ';' in text:
        raw = re.split(r'[,;\r\n]+', text)
    else:
        raw = text.splitlines()
    seen = set()
    out: List[str] = []
    for item in raw:
        name = _clean_token(item)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


class Library:
    """The images of one folder plus their selection state."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.folder: str = ''
        self.entries: List[ImageEntry] = []
        self.version = 0          # bumps on every change (scan or selection)
        self.scan_id = 0          # bumps only when the folder content is replaced
        self._by_path: dict = {}
        self._by_name: dict = {}
        self._by_name_ci: dict = {}
        self._by_stem_ci: dict = {}
        self._dirty: set = set()
        self._listeners: List[Callable[[], None]] = []

    # ------------------------------------------------------------------ events

    def add_listener(self, fn: Callable[[], None]) -> None:
        """Called (on the mutating thread) after every change."""
        self._listeners.append(fn)

    def _changed(self, indices: Optional[Iterable[int]] = None) -> None:
        self.version += 1
        if indices is not None:
            self._dirty.update(indices)
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                pass

    def pop_dirty(self) -> set:
        """Indices whose selection changed since the previous call."""
        with self.lock:
            d = self._dirty
            self._dirty = set()
            return d

    # ---------------------------------------------------------------- scanning

    @staticmethod
    def scan_folder(folder: str) -> List[ImageEntry]:
        """Read a folder and return its images in natural order.

        Safe to call from a background thread; nothing here touches the
        library instance.
        """
        found = []
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if ext not in IMAGE_EXTS:
                    continue
                try:
                    st = entry.stat()
                    mtime_ns, size = st.st_mtime_ns, st.st_size
                except OSError:
                    mtime_ns, size = 0, 0
                found.append(ImageEntry(entry.path, entry.name, 0, mtime_ns, size))
        found.sort(key=lambda e: natural_key(e.name))
        for i, e in enumerate(found):
            e.index = i
        return found

    def set_entries(self, folder: str, entries: Sequence[ImageEntry]) -> None:
        """Replace the folder contents (result of ``scan_folder``)."""
        with self.lock:
            self.folder = folder
            self.entries = list(entries)
            self._by_path = {}
            self._by_name = {}
            self._by_name_ci = {}
            self._by_stem_ci = {}
            for i, e in enumerate(self.entries):
                e.index = i
                self._by_path[e.path] = e
                self._by_name[e.name] = e
                self._by_name_ci.setdefault(e.name.lower(), []).append(e)
                self._by_stem_ci.setdefault(e.stem.lower(), []).append(e)
            self.scan_id += 1
            self._dirty = set()
            self._changed()

    def load(self, folder: str) -> None:
        """Scan ``folder`` synchronously and install the result."""
        self.set_entries(folder, self.scan_folder(folder))

    # ----------------------------------------------------------------- queries

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, index: int) -> Optional[ImageEntry]:
        if 0 <= index < len(self.entries):
            return self.entries[index]
        return None

    def by_path(self, path: str) -> Optional[ImageEntry]:
        return self._by_path.get(path)

    def filtered(self, search: str = '', selected_only: bool = False) -> List[ImageEntry]:
        """Entries matching the sidebar filter, in folder order."""
        search = (search or '').lower()
        with self.lock:
            if not search and not selected_only:
                return list(self.entries)
            out = []
            for e in self.entries:
                if selected_only and not e.selected:
                    continue
                if search and search not in e.name.lower():
                    continue
                out.append(e)
            return out

    def selected(self) -> List[ImageEntry]:
        with self.lock:
            return [e for e in self.entries if e.selected]

    def selected_count(self) -> int:
        with self.lock:
            return sum(1 for e in self.entries if e.selected)

    # --------------------------------------------------------------- selection

    def set_selected(self, indices: Iterable[int], value: bool) -> None:
        with self.lock:
            changed = []
            for i in indices:
                e = self.get(i)
                if e is not None and e.selected != value:
                    e.selected = value
                    changed.append(i)
            if changed:
                self._changed(changed)

    def toggle(self, index: int) -> Optional[bool]:
        with self.lock:
            e = self.get(index)
            if e is None:
                return None
            e.selected = not e.selected
            self._changed([index])
            return e.selected

    def select_range(self, a: int, b: int) -> None:
        """File-explorer style range select: never deselects."""
        lo, hi = sorted((a, b))
        self.set_selected(range(max(0, lo), min(len(self.entries) - 1, hi) + 1), True)

    def clear_selection(self) -> None:
        with self.lock:
            changed = [e.index for e in self.entries if e.selected]
            for i in changed:
                self.entries[i].selected = False
            if changed:
                self._changed(changed)

    # ----------------------------------------------------------- import/export

    def export_names(self, include_ext: bool) -> List[str]:
        return [e.name if include_ext else e.stem for e in self.selected()]

    def export_to_file(self, path: str, include_ext: bool) -> int:
        names = self.export_names(include_ext)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            for n in names:
                f.write(n + '\n')
        return len(names)

    def match_names(self, names: Iterable[str]) -> tuple:
        """Resolve names to entries whether or not they carry an extension.

        Resolution order for each token: exact file name, case-insensitive
        file name, then case-insensitive stem (so ``photo`` or ``photo.jpeg``
        both find ``photo.jpg``). Returns ``(matched_indices, unmatched)``.
        """
        matched: List[int] = []
        seen = set()
        unmatched: List[str] = []
        with self.lock:
            for raw in names:
                name = _clean_token(raw)
                if not name:
                    continue
                hits = []
                exact = self._by_name.get(name)
                if exact is not None:
                    hits = [exact]
                else:
                    hits = self._by_name_ci.get(name.lower()) or []
                if not hits:
                    stem = os.path.splitext(name)[0] if os.path.splitext(name)[1].lower() in IMAGE_EXTS else name
                    hits = self._by_stem_ci.get(stem.lower()) or []
                    if not hits and stem != name:
                        hits = self._by_stem_ci.get(name.lower()) or []
                if not hits:
                    unmatched.append(name)
                    continue
                for e in hits:
                    if e.index not in seen:
                        seen.add(e.index)
                        matched.append(e.index)
        return matched, unmatched

    def import_names(self, names: Iterable[str]) -> tuple:
        """Select every entry named in ``names``. Returns ``(count, unmatched)``."""
        matched, unmatched = self.match_names(names)
        self.set_selected(matched, True)
        return len(matched), unmatched

    def import_text(self, text: str) -> tuple:
        return self.import_names(parse_name_list(text))

    def import_file(self, path: str) -> tuple:
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
            return self.import_text(f.read())
