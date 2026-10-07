"""The Tk desktop application.

Performance model (what changed versus the original single-file viewer):

* The grid is *virtualised*. Canvas items exist only for tiles near the
  viewport and are updated in place; scrolling, a thumbnail arriving or a
  selection toggle never clears and rebuilds the canvas.
* Hit-testing (hover, click) is a per-column binary search, not a scan over
  every image, so a 20 000-image folder costs the same as 20.
* Tk ``PhotoImage`` objects are cached per (path, width); building one is the
  single most expensive main-thread operation and now happens once per thumb.
* The first paint waits only for the first few screens of aspect ratios, read
  in parallel; the rest stream in and the layout is re-anchored so the view
  never jumps while they arrive.
* Single view draws a cached thumbnail instantly, decodes the real image on a
  worker (JPEG draft mode when the screen needs fewer pixels), and renders only
  the visible crop when zoomed, so 500 % on a 40 MP photo stays smooth and
  panning moves an existing canvas item instead of re-resizing.
"""

from __future__ import annotations

import bisect
import json
import os
import queue
import sys
import re
import threading
import time
import webbrowser
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, ttk
from tkinter import font as tkfont

from PIL import Image, ImageTk

from .library import Library, ImageEntry
from .thumbs import ThumbnailService, LRUBytes, open_scaled, open_full, read_aspect, DEFAULT_ASPECT

FIRST_ASPECT_BATCH = 48       # aspects attempted before first paint ...
FIRST_ASPECT_BUDGET = 0.15    # ... within this many seconds, never longer
TREE_CHUNK = 200              # sidebar rows inserted per tick
TREE_CHUNK_MS = 40            # gap between ticks: Tk repaints only when its queue is idle
ASPECT_RELAYOUT_INTERVAL = 0.3  # seconds between relayouts while aspects stream in
WHEEL_STEP_PX = 120           # grid pixels per mouse-wheel notch (at 100 % scale)
PREFETCH_SCREENS = 1.0        # thumbnails requested beyond the viewport
SETTLE_MS = 90                # thumbnails are requested once scrolling pauses this long
ZOOM_MIN, ZOOM_MAX, ZOOM_STEP = 0.25, 5.0, 0.25

IS_WIN = sys.platform == 'win32'
IS_MAC = sys.platform == 'darwin'


def detect_ui_scale(root: tk.Tk, override: Optional[float] = None) -> float:
    """Pixels per CSS-ish 96-dpi pixel for this display.

    Tk sizes fonts in points and scales them with the display DPI, but every
    pixel dimension in a layout stays fixed, which is why a 44 px toolbar
    clips 15 pt buttons on a 200 % display. Everything here goes through
    ``px()`` with this factor. Override with ``--scale`` or IMAGEFLOW_SCALE.
    """
    if override is None:
        env = os.environ.get('IMAGEFLOW_SCALE')
        if env:
            try:
                override = float(env)
            except ValueError:
                override = None
    if override:
        scale = max(0.5, min(4.0, override))
        try:
            root.tk.call('tk', 'scaling', scale * 96.0 / 72.0)
        except tk.TclError:
            pass
        return scale
    try:
        dpi = float(root.winfo_fpixels('1i'))
    except tk.TclError:
        dpi = 96.0
    scale = dpi / 96.0
    if not IS_WIN and not IS_MAC and abs(scale - 1.0) < 0.05:
        # X11 without Xft.dpi reports 96 on HiDPI screens; honour the desktop's hint.
        for var in ('GDK_SCALE', 'QT_SCALE_FACTOR'):
            try:
                hint = float(os.environ.get(var, '') or 0)
            except ValueError:
                hint = 0
            if hint > 1.0:
                scale = hint
                try:
                    root.tk.call('tk', 'scaling', scale * 96.0 / 72.0)
                except tk.TclError:
                    pass
                break
    return max(0.75, min(4.0, scale))


class Tile:
    __slots__ = ('entry', 'fi', 'x', 'y', 'w', 'h')

    def __init__(self, entry: ImageEntry, fi: int, x: int, y: int, w: int, h: int):
        self.entry = entry
        self.fi = fi
        self.x, self.y, self.w, self.h = x, y, w, h


class Drawn:
    """Canvas item ids for one on-screen tile."""
    __slots__ = ('img', 'ph', 'sel', 'ring', 'key', 'broken', 'mark')

    def __init__(self):
        self.img = None
        self.ph = None
        self.mark = None
        self.sel = None
        self.ring = None
        self.key = None
        self.broken = False


class Decoded:
    __slots__ = ('im', 'full_size', 'is_full')

    def __init__(self, im: Image.Image, full_size: Tuple[int, int], is_full: bool):
        self.im = im
        self.full_size = full_size
        self.is_full = is_full


class ImageGallery:
    _CURSOR_FOR_EDGE = {
        'n': 'sb_v_double_arrow', 's': 'sb_v_double_arrow',
        'e': 'sb_h_double_arrow', 'w': 'sb_h_double_arrow',
        'nw': 'size_nw_se', 'se': 'size_nw_se',
        'ne': 'size_ne_sw', 'sw': 'size_ne_sw',
    }

    def __init__(self, root: tk.Tk, library: Optional[Library] = None,
                 service: Optional[ThumbnailService] = None, cli_args=None,
                 web_host: str = '127.0.0.1', web_port: int = 6001,
                 ui_scale: Optional[float] = None):
        self.root = root
        self.root.title("Gallery Viewer")
        self.cli_args = cli_args
        self.lib = library or Library()
        self.service = service or ThumbnailService()
        self.scale = detect_ui_scale(root, ui_scale or getattr(cli_args, 'scale', None))
        self.EDGE_PX = self.px(8)
        self.MIN_W, self.MIN_H = self.px(320), self.px(240)
        self.settings = self._load_settings()
        if isinstance(self.settings.get('web_port'), int) and getattr(cli_args, 'port_given', False) is False:
            self.web_port = self.settings['web_port']
        self._apply_window_geometry()
        self.web_host, self.web_port = web_host, web_port
        self.web_server = None

        # View state
        self.filtered: List[ImageEntry] = []
        self.current_index = 0
        self.zoom_level = 1.0
        self.sidebar_visible = True
        self.dark_mode = bool(self.settings.get('dark_mode', True))
        self.fullscreen_mode = False
        self.grid_mode = True
        self.single_view_mode = False
        self.true_fullscreen = False
        self.saved_scroll_pos = 0.0
        self.pan_x = self.pan_y = 0
        self.pan_start_x = self.pan_start_y = 0
        self.last_clicked_idx: Optional[int] = None
        self.num_columns = int(self.settings.get('columns', 4)) if self.settings.get('columns', 4) in (3, 4, 5, 6) else 4
        self.gap = self.px(4)
        self.loading = False

        # Grid state
        self.tiles: List[Tile] = []
        self.cols: List[List[Tile]] = []
        self.col_ys: List[List[int]] = []
        self.col_w = 0
        self.total_h = 0
        self.ncols = 4
        self.drawn: Dict[int, Drawn] = {}
        self._drawn_by_key: Dict[Tuple[str, int], int] = {}
        self._fi_by_index: Dict[int, int] = {}
        self._photos: 'OrderedDict[Tuple[str, int], ImageTk.PhotoImage]' = OrderedDict()
        self._sync_scheduled = False
        self._scroll_target = None
        self._scroll_anim = None
        self._last_viewport_move = 0.0
        self._last_viewport_top = -1.0
        self._settle_after = None
        self._relayout_after = None
        self._layout_width = 0
        self.focus_highlight_path: Optional[str] = None
        self._focus_highlight_after = None
        self._empty_text_id = None

        # Single view state
        self._view_seq = 0
        self._decoded = LRUBytes(384 * 1024 * 1024)
        self._decode_q: 'queue.LifoQueue' = queue.LifoQueue()
        self._decode_results: 'queue.Queue' = queue.Queue()
        self._decode_inflight: set = set()
        self._decode_wanted: set = set()
        self._single_item = None
        self._single_photo = None
        self._single_render_after = None
        self._single_src: Optional[Decoded] = None
        self._single_entry: Optional[ImageEntry] = None
        self._single_last = None
        self._single_fit = None
        threading.Thread(target=self._decode_worker, name="decode", daemon=True).start()

        # Hover / focus-mode UI state
        self.hover_label = None
        self.hover_fi = None
        self._ghost_items: Dict[str, Tuple[int, int]] = {}
        self._focus_motion_bind = self._focus_leave_bind = None
        self._focus_ctrl_visible = {'prev': False, 'next': False, 'exit': False}
        self._resize_edge = self._resize_active = self._resize_start = None
        self._resize_motion_id = self._resize_press_id = None
        self._resize_drag_id = self._resize_release_id = None

        # Folder loading
        self._scan_token = 0
        self._scan_results: 'queue.Queue' = queue.Queue()
        self._aspects_dirty = False
        self._last_aspect_relayout = 0.0
        self._seen_version = self.lib.version
        self._seen_scan_id = self.lib.scan_id
        self._tree_token = 0
        self._tree_rows = 0
        self._search_after = None
        self._pending_txt: Optional[str] = None
        self._pending_jump: Optional[str] = None

        self.colors = {
            'light': {'bg': '#ffffff', 'sidebar': '#f3f3f3', 'header': '#fafafa',
                      'text': '#1a1a1a', 'button': '#2563eb', 'btn_txt': '#ffffff',
                      'btn_hover': '#1d4ed8', 'subtle_btn': '#fafafa',
                      'subtle_btn_hover': '#e6e6e6', 'subtle_btn_txt': '#1a1a1a',
                      'border': '#dcdcdc', 'placeholder': '#ececec'},
            'dark': {'bg': '#181818', 'sidebar': '#1f1f1f', 'header': '#242424',
                     'text': '#e8e8e8', 'button': '#2563eb', 'btn_txt': '#ffffff',
                     'btn_hover': '#3b82f6', 'subtle_btn': '#242424',
                     'subtle_btn_hover': '#333333', 'subtle_btn_txt': '#e8e8e8',
                     'border': '#2e2e2e', 'placeholder': '#232323'},
        }
        family = "Segoe UI" if IS_WIN else tkfont.nametofont("TkDefaultFont").actual("family")
        self.font_ui = (family, 10)
        self.font_ui_bold = (family, 10, "bold")
        self.font_ui_lg = (family, 11)
        self._tip_font = tkfont.Font(family="Sans", size=10)
        self._ui_font = tkfont.Font(font=self.font_ui)
        self._toolbar_narrow = False
        self._sidebar_w = self.px(int(self.settings.get('sidebar_width', 280)))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.build_ui()
        self.apply_theme()

        self.root.bind("<Escape>", self.on_escape)
        self.root.bind("<F11>", lambda e: self.toggle_true_fullscreen())
        self.root.bind("<Left>", lambda e: self._key(self.prev_img))
        self.root.bind("<Right>", lambda e: self._key(self.next_img))
        self.root.bind("<s>", lambda e: self._key(self.toggle_current_selection))
        self.root.bind("<S>", lambda e: self._key(self.toggle_current_selection))
        self.root.bind("<space>", lambda e: self._key(self.toggle_current_selection))

        if not self.settings.get('sidebar_visible', True):
            self.toggle_sidebar()
        self.root.after(30, self._pump)
        if self.cli_args is not None:
            self.root.after(50, self.process_cli_args)

    # ================================================================ helpers

    def px(self, n: float) -> int:
        """Scale a 96-dpi pixel measure to this display."""
        return max(1, int(round(n * self.scale))) if n else 0

    # ----- settings --------------------------------------------------------

    def _settings_path(self) -> Optional[str]:
        disk = getattr(self.service, 'disk', None)
        if disk is None or not disk.enabled:
            return None
        return os.path.join(os.path.dirname(disk.dir), 'settings.json')

    def _load_settings(self) -> dict:
        p = self._settings_path()
        if not p or not os.path.exists(p):
            return {}
        try:
            with open(p, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        p = self._settings_path()
        if not p:
            return
        try:
            geometry = self.root.geometry() if not self.true_fullscreen and not self.fullscreen_mode else self.settings.get('geometry')
            data = {
                'geometry': geometry,
                'dark_mode': self.dark_mode,
                'columns': self.num_columns if self.num_columns in (3, 4, 5, 6) else 4,
                'sidebar_visible': self.sidebar_visible,
                'sidebar_width': int(round((self.sidebar.winfo_width() if self.sidebar_visible else self._sidebar_w) / self.scale)),
                'export_with_ext': bool(self.export_with_ext.get()),
                'scale_seen': self.scale,
                'web_port': self.web_port,
            }
            tmp = p + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, p)
        except Exception:
            pass

    def _apply_window_geometry(self) -> None:
        """Open at a size that fits this screen (or where the user left it)."""
        root = self.root
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.minsize(self.px(700), self.px(460))
        saved = self.settings.get('geometry')
        if isinstance(saved, str):
            m = re.match(r'^(\d+)x(\d+)([+-]\d+)([+-]\d+)$', saved)
            if m:
                w, h, x, y = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                if self.px(500) <= w <= sw and self.px(300) <= h <= sh and -50 <= x < sw - 100 and -50 <= y < sh - 100:
                    root.geometry(saved)
                    return
        w = min(int(sw * 0.84), self.px(1500))
        h = min(int(sh * 0.84), self.px(950))
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 2 - self.px(16))
        root.geometry(f"{w}x{h}+{x}+{y}")

    def _key(self, fn):
        """Keyboard shortcuts for single view that must not fire while typing."""
        if not self.single_view_mode:
            return
        try:
            if isinstance(self.root.focus_get(), (tk.Entry, ttk.Entry)):
                return
        except (KeyError, tk.TclError):
            pass
        fn()

    def get_color(self, key):
        return self.colors['dark' if self.dark_mode else 'light'][key]

    def _set_info(self, text: str):
        self._info_full = text
        self._fit_info_text()

    def _fit_info_text(self):
        """Show as much of the status text as fits, ending in an ellipsis
        rather than a cut-off word."""
        full = getattr(self, '_info_full', '')
        avail = self.info.winfo_width() - self.px(4)
        if avail <= 10 or self._ui_font.measure(full) <= avail:
            self.info.config(text=full)
            return
        lo, hi = 0, len(full)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._ui_font.measure(full[:mid] + '…') <= avail:
                lo = mid
            else:
                hi = mid - 1
        self.info.config(text=(full[:lo].rstrip() + '…') if lo > 0 else '…')

    # ============================================================== CLI start

    def process_cli_args(self):
        a = self.cli_args
        image = getattr(a, 'image', None)
        folder = getattr(a, 'folder', None)
        txt = getattr(a, 'txt', None)
        if image:
            image_path = os.path.abspath(image)
            if os.path.isfile(image_path):
                self._pending_jump = image_path
                self._pending_txt = os.path.abspath(txt) if txt else None
                self.open_folder(os.path.dirname(image_path))
                return
        if folder:
            folder_path = os.path.abspath(folder)
            if os.path.isdir(folder_path):
                self._pending_txt = os.path.abspath(txt) if txt else None
                self.open_folder(folder_path)
                return
        if txt:
            txt_path = os.path.abspath(txt)
            if os.path.isfile(txt_path):
                self._pending_txt = txt_path
                self.open_folder(os.path.dirname(txt_path))

    def jump_to_image(self, image_path: str):
        e = self.lib.by_path(image_path)
        if e is None:
            return
        fi = self._fi_by_index.get(e.index)
        if fi is None:
            return
        self.show_single_img(fi)

    def select_from_txt(self, txt_file: str):
        """Select every image named in a list file, with or without extensions."""
        try:
            count, unmatched = self.lib.import_file(txt_file)
        except OSError as err:
            self._set_info(f"Could not read list: {err}")
            return 0
        if count:
            if self.view_mode.get() != "selected":
                self.view_mode.set("selected")   # trace -> update_view
            else:
                self.update_view()
        msg = f"Imported {count} selected from {os.path.basename(txt_file)}"
        if unmatched:
            msg += f" ({len(unmatched)} not in folder)"
        self._set_info(msg)
        return count

    def import_list(self):
        if not self.lib.entries:
            self._set_info("Open a folder first")
            return
        path = filedialog.askopenfilename(
            title="Import Selected Filenames",
            filetypes=[("Text files", "*.txt *.csv"), ("All files", "*.*")])
        if path:
            self.select_from_txt(path)

    # ================================================================= build

    def build_ui(self):
        px = self.px
        self.main = tk.Frame(self.root)
        self.main.pack(fill=tk.BOTH, expand=True)

        # Toolbar: natural height (so scaled fonts are never clipped) and two
        # rows when the window is too narrow for one.
        self.toolbar = tk.Frame(self.root)
        self.toolbar.pack(side=tk.TOP, fill=tk.X, before=self.main)
        self.tb_left = left = tk.Frame(self.toolbar)
        self.tb_right = right = tk.Frame(self.toolbar)
        left.pack(side=tk.LEFT, padx=px(8), pady=px(6))
        right.pack(side=tk.RIGHT, padx=px(8), pady=px(6))
        self.toolbar.bind("<Configure>", lambda e: self._layout_toolbar())

        def mk(parent, text, cmd, **kw):
            return tk.Button(parent, text=text, command=cmd, padx=px(kw.pop('padx', 10)), pady=px(6),
                             cursor="hand2", font=self.font_ui, **kw)
        self.select_folder_btn = mk(left, "📁  Open Folder", self.select_folder, padx=12)
        self.select_folder_btn.pack(side=tk.LEFT, padx=px(3))
        self.deselect_btn = mk(left, "Clear Selection", self.deselect_all, state=tk.DISABLED)
        self.deselect_btn.pack(side=tk.LEFT, padx=px(3))
        self.export_btn = mk(left, "Export", self.export_selected, state=tk.DISABLED)
        self.export_btn.pack(side=tk.LEFT, padx=px(3))
        self.import_btn = mk(left, "Import", self.import_list)
        self.import_btn.pack(side=tk.LEFT, padx=px(3))
        self.back_btn = mk(left, "◀  Grid", self.back_to_grid)

        self.full_btn = mk(right, "Focus", self.toggle_fullscreen)
        self.full_btn.pack(side=tk.RIGHT, padx=px(3))
        self.dark_btn = mk(right, "☀" if self.dark_mode else "☾", self.toggle_dark)
        self.dark_btn.pack(side=tk.RIGHT, padx=px(3))
        self.side_btn = mk(right, "Panel", self.toggle_sidebar)
        self.side_btn.pack(side=tk.RIGHT, padx=px(3))
        self.web_btn = mk(right, "Web", self.open_web)
        self.web_btn.pack(side=tk.RIGHT, padx=px(3))
        # Port for the web view: editable so a busy port or a preferred one
        # can be set without the command line.
        self.port_var = tk.StringVar(value=str(self.web_port))
        self.port_entry = tk.Entry(right, textvariable=self.port_var, width=6, font=self.font_ui,
                                   relief=tk.FLAT, borderwidth=1, justify=tk.CENTER)
        self.port_entry.pack(side=tk.RIGHT, padx=(px(3), 0), ipady=px(4))
        self.port_entry.bind("<Return>", lambda e: self.open_web())
        self.port_lbl = tk.Label(right, text="Port", font=self.font_ui)
        self.port_lbl.pack(side=tk.RIGHT, padx=(px(6), px(2)))


        # Sidebar and display live in a paned window so the sidebar width can
        # be dragged; it is remembered between runs.
        self.paned = tk.PanedWindow(self.main, orient=tk.HORIZONTAL, sashwidth=px(6), sashrelief=tk.FLAT,
                                    bd=0, opaqueresize=True, showhandle=False, sashpad=0)
        self.paned.pack(fill=tk.BOTH, expand=True)
        self.sidebar = tk.Frame(self.paned, width=self._sidebar_w)
        self.display = tk.Frame(self.paned)
        self.paned.add(self.sidebar, minsize=px(200), width=self._sidebar_w, stretch='never')
        self.paned.add(self.display, minsize=px(320), stretch='always')

        sf = tk.Frame(self.sidebar)
        sf.pack(fill=tk.X, padx=px(12), pady=(px(12), px(8)))
        tk.Label(sf, text="Search", font=self.font_ui_bold).pack(anchor=tk.W, pady=(0, px(4)))
        self.search_var = tk.StringVar()
        self.search_var.trace_add('write', lambda *a: self._schedule_search())
        self.search_entry = tk.Entry(sf, textvariable=self.search_var, font=self.font_ui,
                                     relief=tk.FLAT, borderwidth=1)
        self.search_entry.pack(fill=tk.X, ipady=px(4))

        mf = tk.Frame(self.sidebar)
        mf.pack(fill=tk.X, padx=px(12), pady=px(8))
        tk.Label(mf, text="View", font=self.font_ui_bold).pack(anchor=tk.W, pady=(0, px(4)))
        self.view_mode = tk.StringVar(value="all")
        self.view_mode.trace_add('write', self.on_view_mode_change)
        tk.Radiobutton(mf, text="All", variable=self.view_mode, value="all",
                       cursor="hand2", font=self.font_ui).pack(anchor=tk.W, pady=1)
        tk.Radiobutton(mf, text="Selected Only", variable=self.view_mode, value="selected",
                       cursor="hand2", font=self.font_ui).pack(anchor=tk.W, pady=1)

        ef = tk.Frame(self.sidebar)
        ef.pack(fill=tk.X, padx=px(12), pady=(px(4), px(4)))
        tk.Label(ef, text="Export", font=self.font_ui_bold).pack(anchor=tk.W, pady=(0, px(4)))
        self.export_with_ext = tk.BooleanVar(value=bool(self.settings.get('export_with_ext', False)))
        tk.Checkbutton(ef, text="Include File Extensions", variable=self.export_with_ext,
                       cursor="hand2", font=self.font_ui).pack(anchor=tk.W, pady=1)

        cf = tk.Frame(self.sidebar)
        cf.pack(fill=tk.X, padx=px(12), pady=px(8))
        tk.Label(cf, text="Columns", font=self.font_ui_bold).pack(anchor=tk.W, pady=(0, px(4)))
        col_frame = tk.Frame(cf)
        col_frame.pack(fill=tk.X)
        self.col_var = tk.IntVar(value=self.num_columns)
        self.col_radios = {}
        for i in (3, 4, 5, 6):
            rb = tk.Radiobutton(col_frame, text=str(i), variable=self.col_var, value=i,
                                command=self.on_column_change, cursor="hand2", font=self.font_ui)
            rb.pack(side=tk.LEFT, padx=px(4))
            self.col_radios[i] = rb

        tk.Label(self.sidebar, text="Files", font=self.font_ui_bold).pack(padx=px(12), pady=(px(8), px(4)), anchor=tk.W)
        lf = tk.Frame(self.sidebar)
        lf.pack(fill=tk.BOTH, expand=True, padx=px(10), pady=px(5))
        scroll = tk.Scrollbar(lf)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree = ttk.Treeview(lf, columns=('num', 'sel', 'name'), show='headings',
                                 yscrollcommand=scroll.set, height=15, selectmode='browse')
        scroll.config(command=self.tree.yview)
        self.tree.column('num', width=px(44), anchor=tk.CENTER, minwidth=px(36), stretch=False)
        self.tree.column('sel', width=px(34), anchor=tk.CENTER, minwidth=px(30), stretch=False)
        self.tree.column('name', width=px(170), anchor=tk.W, minwidth=px(80))
        self.tree.heading('num', text='#', anchor=tk.CENTER)
        self.tree.heading('sel', text='✓', anchor=tk.CENTER)
        self.tree.heading('name', text='Filename', anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind('<ButtonRelease-1>', self.on_tree_click)
        self.tree.bind('<Double-1>', self.on_tree_dbl)

        canvas_frame = tk.Frame(self.display)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=px(10), pady=px(10))
        self.scrollbar = tk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=self._on_scrollbar)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas = tk.Canvas(canvas_frame, highlightthickness=0, cursor="hand2",
                                yscrollcommand=self.scrollbar.set, yscrollincrement=1)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas.bind("<ButtonPress-1>", self.on_click)
        self.canvas.bind("<Double-Button-1>", self.on_double_click)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Button-4>", self.on_wheel)
        self.canvas.bind("<Button-5>", self.on_wheel)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Motion>", self.on_canvas_motion)
        self.canvas.bind("<Leave>", lambda e: self._hide_hover_tooltip())

        self.nav_frame = tk.Frame(self.display)
        self.nav_frame.pack(fill=tk.X, padx=px(8), pady=px(6))
        self.nav_frame.bind("<Configure>", lambda e: self.root.after_idle(self._layout_nav))
        self._nav_counter_hidden = False
        self._nav_zoom_caption_hidden = False
        self.prev_btn = tk.Button(self.nav_frame, text="◀  Previous", command=self.prev_img,
                                  padx=px(14), pady=px(6), cursor="hand2", font=self.font_ui)
        self.select_current_btn = tk.Button(self.nav_frame, text="☆  Select  (S)",
                                            command=self.toggle_current_selection,
                                            padx=px(12), pady=px(6), cursor="hand2", font=self.font_ui)
        # width=1 lets the info text be the element that shrinks when the bar
        # is narrow; the controls keep their size.
        self.info = tk.Label(self.nav_frame, text="Open a folder to begin", font=self.font_ui, anchor=tk.W, width=1)
        self._info_full = "Open a folder to begin"
        self.info.bind("<Configure>", lambda e: self._fit_info_text())
        self.sel_counter = tk.Label(self.nav_frame, text="", font=self.font_ui)
        self.next_btn = tk.Button(self.nav_frame, text="Next  ▶", command=self.next_img,
                                  padx=px(14), pady=px(6), cursor="hand2", font=self.font_ui)
        # Zoom controls sit in the bottom bar with the other single-view
        # controls, as one group that always reads "Zoom − 100% +". Keeping
        # them out of the toolbar means it stays one row on a laptop screen.
        self.zoom_box = tk.Frame(self.nav_frame)
        self.zoom_txt = tk.Label(self.zoom_box, text="Zoom", font=self.font_ui)
        self.zoom_out = tk.Button(self.zoom_box, text="−", command=self.zoom_out_fn, width=3,
                                  cursor="hand2", pady=px(5), font=self.font_ui)
        self.zoom_val = tk.Label(self.zoom_box, text="100%", width=5, font=self.font_ui, anchor=tk.CENTER)
        self.zoom_in = tk.Button(self.zoom_box, text="+", command=self.zoom_in_fn, width=3,
                                 cursor="hand2", pady=px(5), font=self.font_ui)
        self.zoom_txt.pack(side=tk.LEFT, padx=(px(4), px(6)))
        self.zoom_out.pack(side=tk.LEFT)
        self.zoom_val.pack(side=tk.LEFT, padx=px(2))
        self.zoom_in.pack(side=tk.LEFT)
        self.update_ui_state()

    def _layout_toolbar(self):
        """One row when it fits, two rows (actions above, view controls below)
        when the window is narrower than the buttons need."""
        width = self.toolbar.winfo_width()
        if width <= 1:
            return
        need = self.tb_left.winfo_reqwidth() + self.tb_right.winfo_reqwidth() + self.px(28)
        narrow = need > width
        if narrow == self._toolbar_narrow:
            return
        self._toolbar_narrow = narrow
        px = self.px
        self.tb_left.pack_forget()
        self.tb_right.pack_forget()
        if narrow:
            self.tb_left.pack(side=tk.TOP, anchor=tk.W, padx=px(8), pady=(px(6), px(2)))
            self.tb_right.pack(side=tk.TOP, anchor=tk.E, padx=px(8), pady=(0, px(6)))
        else:
            self.tb_left.pack(side=tk.LEFT, padx=px(8), pady=px(6))
            self.tb_right.pack(side=tk.RIGHT, padx=px(8), pady=px(6))

    # ================================================================= theme

    def apply_theme(self):
        bg, sb, hd, tx = (self.get_color(k) for k in ('bg', 'sidebar', 'header', 'text'))
        btn, btn_txt = self.get_color('button'), self.get_color('btn_txt')
        for w in (self.root, self.main, self.display, self.canvas, self.nav_frame):
            w.configure(bg=bg)
        self.paned.configure(bg=self.get_color('border'))
        self.toolbar.configure(bg=hd)
        self.sidebar.configure(bg=sb)
        self.info.configure(bg=bg, fg=tx)
        self.sel_counter.configure(bg=bg, fg=tx)
        sub_bg, sub_hover, sub_fg = (self.get_color(k) for k in ('subtle_btn', 'subtle_btn_hover', 'subtle_btn_txt'))
        accent_hover = self.get_color('btn_hover')
        def theme_toolbar(container):
            for w in container.winfo_children():
                if isinstance(w, tk.Button):
                    self._style_btn(w, sub_bg, sub_hover, sub_fg)
                elif isinstance(w, tk.Label):
                    w.configure(bg=hd, fg=tx)
                elif isinstance(w, tk.Entry):
                    ebg = '#2a2a2a' if self.dark_mode else '#ffffff'
                    w.configure(bg=ebg, fg=tx, insertbackground=tx, highlightthickness=1,
                                highlightbackground=self.get_color('border'), highlightcolor=self.get_color('button'))
                elif isinstance(w, tk.Frame):
                    w.configure(bg=hd)
                    theme_toolbar(w)
        self.toolbar.configure(bg=hd)
        theme_toolbar(self.toolbar)
        for child in self.sidebar.winfo_children():
            self._apply_theme_recursive(child, sb, tx)
        for b in (self.prev_btn, self.next_btn, self.select_current_btn):
            self._style_btn(b, btn, accent_hover, btn_txt)
        self.zoom_box.configure(bg=bg)
        for w in (self.zoom_txt, self.zoom_val):
            w.configure(bg=bg, fg=tx)
        for b in (self.zoom_out, self.zoom_in):
            self._style_btn(b, self.get_color('subtle_btn_hover'), self.get_color('border'), tx)
        style = ttk.Style()
        style.theme_use('default')
        rowheight = self._ui_font.metrics('linespace') + self.px(9)
        style.configure("Treeview", font=self.font_ui)
        style.configure("Treeview.Heading", font=self.font_ui_bold)
        if self.dark_mode:
            style.configure("Treeview", background="#1f1f1f", foreground="#e8e8e8",
                            fieldbackground="#1f1f1f", rowheight=rowheight, borderwidth=0)
            style.map('Treeview', background=[('selected', '#2563eb')])
            style.configure("Treeview.Heading", background="#242424", foreground="#e8e8e8", relief="flat")
        else:
            style.configure("Treeview", background="#ffffff", foreground="#000000",
                            fieldbackground="#ffffff", rowheight=rowheight, borderwidth=0)
            style.map('Treeview', background=[('selected', '#0078d4')])
            style.configure("Treeview.Heading", background="#e8e8e8", foreground="#000000", relief="flat")
        self._restyle_focus_controls()
        # Placeholders and the empty-state text carry theme colours.
        ph = self.get_color('placeholder')
        for d in self.drawn.values():
            if d.ph is not None:
                self.canvas.itemconfig(d.ph, fill=ph)
            if d.mark is not None:
                self.canvas.itemconfig(d.mark, fill=tx)
        if self._empty_text_id is not None:
            self.canvas.itemconfig(self._empty_text_id, fill=tx)

    def _style_btn(self, btn, base_bg, hover_bg, fg):
        try:
            btn.configure(bg=base_bg, fg=fg, activebackground=hover_bg, activeforeground=fg,
                          relief=tk.FLAT, borderwidth=0, highlightthickness=0)
        except tk.TclError:
            return

        def on_enter(_e, b=btn, hb=hover_bg):
            if str(b.cget('state')) != tk.DISABLED:
                b.configure(bg=hb)

        def on_leave(_e, b=btn, bb=base_bg):
            b.configure(bg=bb)

        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)

    def _apply_theme_recursive(self, widget, bg, fg):
        try:
            if isinstance(widget, tk.Label):
                widget.configure(bg=bg, fg=fg)
            elif isinstance(widget, tk.Frame):
                widget.configure(bg=bg)
            elif isinstance(widget, tk.Entry):
                ebg = '#2a2a2a' if self.dark_mode else '#ffffff'
                efg = '#e8e8e8' if self.dark_mode else '#000000'
                widget.configure(bg=ebg, fg=efg, insertbackground=efg, highlightthickness=1,
                                 highlightbackground=self.get_color('border'), highlightcolor=self.get_color('button'))
            elif isinstance(widget, (tk.Radiobutton, tk.Checkbutton)):
                widget.configure(bg=bg, fg=fg, selectcolor=bg, activebackground=bg, activeforeground=fg,
                                 highlightthickness=0, borderwidth=0)
            elif isinstance(widget, tk.Button):
                self._style_btn(widget, self.get_color('button'), self.get_color('btn_hover'),
                                self.get_color('btn_txt'))
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self._apply_theme_recursive(child, bg, fg)

    def toggle_dark(self):
        self.dark_mode = not self.dark_mode
        self.dark_btn.config(text="☀" if self.dark_mode else "☾")
        self.apply_theme()
        if not self.grid_mode:
            self._render_single()

    def _pane_names(self):
        # Tk returns names on some builds and window objects on others.
        return [str(p) for p in self.paned.panes()]

    def _hide_sidebar_pane(self):
        if str(self.sidebar) in self._pane_names():
            w = self.sidebar.winfo_width()
            if w > self.px(100):
                self._sidebar_w = w
            self.paned.forget(self.sidebar)

    def _show_sidebar_pane(self):
        if str(self.sidebar) not in self._pane_names():
            self.paned.add(self.sidebar, before=self.display, minsize=self.px(200),
                           width=self._sidebar_w, stretch='never')

    def toggle_sidebar(self):
        if self.fullscreen_mode:
            # Focus mode hides all chrome; remember the choice for when it ends.
            self.sidebar_visible = not self.sidebar_visible
            return
        if self.sidebar_visible:
            self._hide_sidebar_pane()
        else:
            self._show_sidebar_pane()
        self.sidebar_visible = not self.sidebar_visible

    # ============================================================== UI state

    def update_ui_state(self):
        """Lay out the toolbar and bottom bar for the current mode.

        The bottom bar is packed in priority order: controls first, the
        counter next, the info text last so it is what gives way when the
        window is narrow (the packer squeezes the last widgets first)."""
        px = self.px
        for w in (self.back_btn, self.prev_btn, self.next_btn, self.select_current_btn,
                  self.zoom_box, self.sel_counter, self.info):
            w.pack_forget()
        if self.single_view_mode:
            if not (self.view_mode.get() == "selected" and len(self.filtered) == 1):
                self.back_btn.pack(side=tk.LEFT, padx=px(3))
            many = len(self.filtered) > 1
            if many:
                self.prev_btn.pack(side=tk.LEFT, padx=px(5))
            self.select_current_btn.pack(side=tk.LEFT, padx=px(5))
            if many:
                self.next_btn.pack(side=tk.RIGHT, padx=px(5))
            self.zoom_box.pack(side=tk.RIGHT, padx=(px(4), px(8)))
            self._update_select_btn_label()
        self.sel_counter.pack(side=tk.RIGHT, padx=px(8))
        self.info.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=px(6))
        any_selected = self.lib.selected_count() > 0
        state = tk.NORMAL if any_selected else tk.DISABLED
        self.deselect_btn.config(state=state)
        self.export_btn.config(state=state)
        self._update_selection_counter()
        self._nav_counter_hidden = False
        self._nav_zoom_caption_hidden = False
        self.root.after_idle(self._layout_toolbar)
        self.root.after_idle(self._layout_nav)

    def _layout_nav(self):
        """Drop the least important parts of the bottom bar when it is too
        narrow for everything: first the selection counter, then the word
        "Zoom". Nothing is ever shown half-clipped."""
        width = self.nav_frame.winfo_width()
        if width <= 1:
            return
        px = self.px
        controls = [w for w in (self.back_btn, self.prev_btn, self.select_current_btn, self.next_btn, self.zoom_box)
                    if w.winfo_manager()]
        fixed = sum(w.winfo_reqwidth() + px(10) for w in controls)
        min_info = px(150) if self.single_view_mode else px(120)
        counter_w = self.sel_counter.winfo_reqwidth() + px(16)
        want_counter = fixed + min_info + counter_w <= width or not self.single_view_mode
        if want_counter and self._nav_counter_hidden:
            self.sel_counter.pack(side=tk.RIGHT, padx=px(8), before=self.info)
            self._nav_counter_hidden = False
        elif not want_counter and not self._nav_counter_hidden:
            self.sel_counter.pack_forget()
            self._nav_counter_hidden = True
        if self.single_view_mode:
            caption_w = self.zoom_txt.winfo_reqwidth() + px(10)
            want_caption = fixed + min_info <= width
            if want_caption and self._nav_zoom_caption_hidden:
                self.zoom_txt.pack(side=tk.LEFT, padx=(px(4), px(6)), before=self.zoom_out)
                self._nav_zoom_caption_hidden = False
            elif not want_caption and not self._nav_zoom_caption_hidden:
                self.zoom_txt.pack_forget()
                self._nav_zoom_caption_hidden = True

    def _update_select_btn_label(self):
        if not self.single_view_mode or not (0 <= self.current_index < len(self.filtered)):
            return
        picked = self.filtered[self.current_index].selected
        self.select_current_btn.config(text="★ Selected (S)" if picked else "☆ Select (S)")

    def _update_selection_counter(self):
        total = len(self.lib)
        if total == 0:
            self.sel_counter.config(text="")
        else:
            self.sel_counter.config(text=f"{self.lib.selected_count()} selected / {total} total")

    def on_view_mode_change(self, *args):
        self.update_view()

    def on_column_change(self):
        self.num_columns = self.col_var.get()
        if self.grid_mode:
            self._relayout(keep_anchor=True)

    def _schedule_search(self):
        if self._search_after is not None:
            self.root.after_cancel(self._search_after)
        self._search_after = self.root.after(120, self._run_search)

    def _run_search(self):
        self._search_after = None
        self.update_view()

    # ============================================================== folders

    def select_folder(self):
        folder = filedialog.askdirectory(title="Select Image Folder")
        if folder:
            self.open_folder(folder)

    def open_folder(self, folder: str):
        self._scan_token += 1
        token = self._scan_token
        self.loading = True
        self._set_info("Scanning...")
        # Drop queued work and cached pixels of the previous folder *before*
        # the scan thread starts reading the new folder's aspect ratios.
        self.service.cancel_all()
        self.service.clear_memory()
        self._photos.clear()

        def work():
            try:
                entries = Library.scan_folder(folder)
                # Ratios remembered from an earlier visit cost nothing; for a
                # new folder spend at most a fraction of a second on the first
                # screen, then paint. Everything else streams in.
                self.service.load_aspect_index(folder, entries)
                self.service.read_aspects_now([e.path for e in entries[:FIRST_ASPECT_BATCH]],
                                              threads=8, budget=FIRST_ASPECT_BUDGET)
                self._scan_results.put((token, folder, entries, None))
            except Exception as err:  # unreadable folder
                self._scan_results.put((token, folder, [], err))

        threading.Thread(target=work, name="scan", daemon=True).start()

    def _finish_scan(self, folder, entries, err):
        self.loading = False
        if err is not None:
            self._set_info(f"Error: {err}")
            return
        self.lib.set_entries(folder, entries)
        self._seen_scan_id = self.lib.scan_id
        self._seen_version = self.lib.version
        self._after_library_replaced(entries)

    def _after_library_replaced(self, entries):
        """Shared by local scans and folder changes made from the web viewer."""
        self.root.title(f"Gallery Viewer — {os.path.basename(self.lib.folder) or self.lib.folder}")
        self.last_clicked_idx = None
        self.saved_scroll_pos = 0.0
        if self.single_view_mode:
            self._leave_single_view()
        if self.view_mode.get() == "selected" and self.lib.selected_count() == 0:
            self.view_mode.set("all")  # trace runs update_view
        else:
            self.update_view()
        self._set_info(f"Loaded {len(entries)} images")
        rest = [e.path for e in entries if e.path not in self.service.aspects]
        if rest:
            self.service.ensure_aspects(rest, on_progress=self._on_aspect_progress)
        if self._pending_txt:
            txt, self._pending_txt = self._pending_txt, None
            self.select_from_txt(txt)
        if self._pending_jump:
            path, self._pending_jump = self._pending_jump, None
            self.jump_to_image(path)

    def _on_aspect_progress(self, done, total):
        self._aspects_dirty = True   # consumed by _pump on the Tk thread
        if total and done >= total:
            # Worker thread: persist what was learnt so the next open is instant.
            self.service.save_aspect_index(self.lib.folder, self.lib.entries)

    # ================================================================= view

    def update_view(self):
        if self.loading:
            return
        search = self.search_var.get()
        mode = self.view_mode.get()
        self.filtered = self.lib.filtered(search, mode == "selected")
        self._fi_by_index = {e.index: fi for fi, e in enumerate(self.filtered)}
        self._rebuild_tree()

        if mode == "selected":
            count = len(self.filtered)
            if count in (1, 2, 3):
                for rb in self.col_radios.values():
                    rb.config(state=tk.DISABLED)
                self.col_var.set(0)
            else:
                for rb in self.col_radios.values():
                    rb.config(state=tk.NORMAL)
                if self.col_var.get() == 0:
                    self.col_var.set(4)
                    self.num_columns = 4
            if count == 1:
                self.show_single_img(0)
                return
        else:
            for rb in self.col_radios.values():
                rb.config(state=tk.NORMAL)
            if self.col_var.get() == 0:
                self.col_var.set(4)
                self.num_columns = 4

        if self.single_view_mode:
            # Keep showing the *same* image if it survived the filter; its
            # position in the filtered list may have changed.
            entry = self._single_entry
            new_fi = self._fi_by_index.get(entry.index) if entry is not None else None
            if new_fi is None:
                self._leave_single_view()
            else:
                self.current_index = new_fi
                self._set_info(f"{new_fi + 1} / {len(self.filtered)} - {entry.name}")
        self.update_ui_state()
        if self.grid_mode:
            self._relayout(keep_anchor=False)
            self.canvas.yview_moveto(0)
            self._schedule_sync()
        else:
            self._update_select_btn_label()

    # ---------------------------------------------------------------- tree

    def _rebuild_tree(self):
        self._tree_token += 1
        self.tree.delete(*self.tree.get_children(''))
        self._tree_rows = 0
        self._insert_tree_chunk(self._tree_token)

    def _insert_tree_chunk(self, token):
        if token != self._tree_token:
            return
        start = self._tree_rows
        end = min(start + TREE_CHUNK, len(self.filtered))
        ins = self.tree.insert
        for i in range(start, end):
            e = self.filtered[i]
            ins('', 'end', iid=str(i), values=(i + 1, "✓" if e.selected else "", e.name))
        self._tree_rows = end
        if end < len(self.filtered):
            # A timer gap, not after_idle: back-to-back timers would starve
            # the idle redraw and the grid would not appear until the list
            # was complete.
            self.root.after(TREE_CHUNK_MS, lambda: self._insert_tree_chunk(token))
        elif self.single_view_mode:
            self._focus_tree_on_index(self.current_index)

    def _tree_set_mark(self, fi: int):
        if fi < self._tree_rows:
            e = self.filtered[fi]
            self.tree.set(str(fi), 'sel', "✓" if e.selected else "")

    def _focus_tree_on_index(self, fi: int):
        if not (0 <= fi < self._tree_rows):
            return
        iid = str(fi)
        try:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)
        except tk.TclError:
            pass

    def on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        iid = self.tree.identify_row(event.y)
        if not iid or region != "cell":
            return
        column = self.tree.identify_column(event.x)
        try:
            fi = int(iid)
        except ValueError:
            return
        if not (0 <= fi < len(self.filtered)):
            return
        shift_held = bool(event.state & 0x0001)
        if column == '#2':
            if shift_held and self.last_clicked_idx is not None and 0 <= self.last_clicked_idx < len(self.filtered):
                self._select_range_fi(self.last_clicked_idx, fi)
            else:
                self.lib.toggle(self.filtered[fi].index)
            self.last_clicked_idx = fi
            self._apply_selection_change()
            return
        self.last_clicked_idx = fi
        self._focus_tree_on_index(fi)
        if self.grid_mode:
            self._scroll_grid_to_index(fi)

    def on_tree_dbl(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        try:
            fi = int(iid)
        except ValueError:
            return
        if 0 <= fi < len(self.filtered):
            self.saved_scroll_pos = self.canvas.yview()[0]
            self.show_single_img(fi)

    # ------------------------------------------------------------ selection

    def _select_range_fi(self, a: int, b: int):
        lo, hi = sorted((a, b))
        self.lib.set_selected((self.filtered[i].index for i in range(lo, hi + 1)), True)

    def _apply_selection_change(self):
        """Refresh what a selection change touched: tree marks, tile outlines,
        counters. Falls back to a full update only when the filter depends on
        selection."""
        if self.view_mode.get() == "selected":
            self.lib.pop_dirty()
            self.update_view()
            return
        self._refresh_dirty(self.lib.pop_dirty())

    def _refresh_dirty(self, dirty_indices):
        for idx in dirty_indices:
            fi = self._fi_by_index.get(idx)
            if fi is None:
                continue
            self._tree_set_mark(fi)
            d = self.drawn.get(fi)
            if d is not None:
                self._update_tile_outline(fi, d)
        self.update_ui_state()
        if self.single_view_mode:
            self._update_select_btn_label()

    def toggle_current_selection(self):
        if not self.single_view_mode or not (0 <= self.current_index < len(self.filtered)):
            return
        self.lib.toggle(self.filtered[self.current_index].index)
        self.last_clicked_idx = self.current_index
        self._apply_selection_change()

    def deselect_all(self):
        self.lib.clear_selection()
        self.last_clicked_idx = None
        if self.view_mode.get() != "all":
            self.view_mode.set("all")   # trace -> update_view
            self.lib.pop_dirty()
        else:
            self._refresh_dirty(self.lib.pop_dirty())

    def export_selected(self):
        n = self.lib.selected_count()
        if n == 0:
            self._set_info("No selected images to export")
            return
        path = filedialog.asksaveasfilename(
            title="Export Selected Filenames", defaultextension=".txt",
            initialfile="selected_images.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            count = self.lib.export_to_file(path, bool(self.export_with_ext.get()))
            self._set_info(f"Exported {count} names → {os.path.basename(path)}")
        except OSError as err:
            self._set_info(f"Export failed: {err}")

    # ================================================================ layout

    def _on_canvas_configure(self, event):
        if self.grid_mode:
            if event.width != self._layout_width:
                if self._relayout_after is not None:
                    self.root.after_cancel(self._relayout_after)
                self._relayout_after = self.root.after(60, lambda: self._relayout(keep_anchor=True))
            else:
                self._schedule_sync()
        elif self.single_view_mode:
            self._ensure_source_for_zoom()
            self._schedule_single_render(0)
            if self.fullscreen_mode:
                self._place_ghosts()

    def _columns_now(self) -> int:
        if self.view_mode.get() == "selected":
            n = len(self.filtered)
            if n == 2:
                return 2
            if n == 3:
                return 3
        return self.num_columns if self.num_columns > 0 else 4

    def _anchor(self):
        """(tile, offset) for the tile at the top of the viewport, or None."""
        if not self.tiles or self.total_h <= 0:
            return None
        top = self.canvas.yview()[0] * self.total_h
        best = None
        for c, ys in enumerate(self.col_ys):
            i = bisect.bisect_right(ys, top) - 1
            i = max(0, i)
            if i < len(self.cols[c]):
                t = self.cols[c][i]
                if t.y + t.h < top and i + 1 < len(self.cols[c]):
                    t = self.cols[c][i + 1]
                if best is None or t.y < best.y:
                    best = t
        if best is None:
            return None
        return best.entry.index, best.y - top

    def _relayout(self, keep_anchor: bool):
        self._relayout_after = None
        self._scroll_target = None
        if not self.grid_mode:
            return
        cw = self.canvas.winfo_width()
        if cw <= 1:
            self._relayout_after = self.root.after(60, lambda: self._relayout(keep_anchor))
            return
        anchor = self._anchor() if keep_anchor else None
        self._layout_width = cw
        self._compute_layout(cw)
        # Every drawn item has moved; rebuild the visible set from scratch.
        self._clear_drawn()
        if anchor is not None and self.total_h > 0:
            idx, offset = anchor
            fi = self._fi_by_index.get(idx)
            if fi is not None and fi < len(self.tiles):
                top = max(0, min(self.tiles[fi].y - offset, self.total_h - self.canvas.winfo_height()))
                self.canvas.yview_moveto(top / self.total_h)
        self._sync_viewport()

    def _compute_layout(self, cw: int):
        cols = max(1, self._columns_now())
        gap = self.gap
        col_w = max(self.px(50), (cw - gap * (cols + 1)) // cols)
        heights = [gap] * cols
        tiles: List[Tile] = []
        col_lists: List[List[Tile]] = [[] for _ in range(cols)]
        aspects = self.service.aspects
        for fi, e in enumerate(self.filtered):
            c = heights.index(min(heights))
            a = aspects.get(e.path, DEFAULT_ASPECT)
            h = max(1, int(col_w / a)) if a > 0 else col_w
            t = Tile(e, fi, gap + c * (col_w + gap), heights[c], col_w, h)
            tiles.append(t)
            col_lists[c].append(t)
            heights[c] += h + gap
        self.tiles = tiles
        self.cols = col_lists
        self.col_ys = [[t.y for t in col] for col in col_lists]
        self.col_w = col_w
        self.ncols = cols
        self.total_h = (max(heights) + gap) if tiles else 0
        self.canvas.config(scrollregion=(0, 0, cw, max(self.total_h, 1)))

    def _tile_at(self, cx: float, cy: float) -> Optional[Tile]:
        if not self.tiles:
            return None
        c = int((cx - self.gap) // (self.col_w + self.gap))
        if not (0 <= c < self.ncols):
            return None
        if cx > self.gap + c * (self.col_w + self.gap) + self.col_w:
            return None
        ys = self.col_ys[c]
        i = bisect.bisect_right(ys, cy) - 1
        if i < 0:
            return None
        t = self.cols[c][i]
        return t if t.y <= cy <= t.y + t.h else None

    def _visible_tiles(self, top: float, bottom: float) -> List[Tile]:
        out = []
        for c, ys in enumerate(self.col_ys):
            i = max(0, bisect.bisect_right(ys, top) - 1)
            col = self.cols[c]
            while i < len(col):
                t = col[i]
                if t.y > bottom:
                    break
                if t.y + t.h >= top:
                    out.append(t)
                i += 1
        return out

    # ============================================================== drawing

    def _clear_drawn(self):
        self.canvas.delete("grid_item")
        self.drawn.clear()
        self._drawn_by_key.clear()

    def _on_scrollbar(self, *args):
        self.canvas.yview(*args)
        self._schedule_sync()

    def _schedule_sync(self):
        if not self._sync_scheduled:
            self._sync_scheduled = True
            self.root.after_idle(self._sync_viewport)

    def _photo(self, key, im: Image.Image) -> ImageTk.PhotoImage:
        p = self._photos.get(key)
        if p is None:
            p = ImageTk.PhotoImage(im)
            self._photos[key] = p
            limit = max(300, 4 * len(self.drawn))
            if len(self._photos) > limit:
                for k in list(self._photos.keys()):
                    if len(self._photos) <= limit:
                        break
                    if k not in self._drawn_by_key:
                        del self._photos[k]
        else:
            self._photos.move_to_end(key)
        return p

    def _sync_viewport(self):
        """Make the canvas match the viewport: create, update or drop tiles."""
        self._sync_scheduled = False
        if not self.grid_mode:
            return
        ch = self.canvas.winfo_height()
        if self._empty_text_id is not None:
            self.canvas.delete(self._empty_text_id)
            self._empty_text_id = None
        if not self.tiles:
            self._clear_drawn()
            self._empty_text_id = self.canvas.create_text(
                self.canvas.winfo_width() // 2, ch // 2,
                text="No images\nSelect a folder" if not self.lib.entries else "No images match",
                font=("Sans", 14), fill=self.get_color('text'), justify=tk.CENTER)
            return
        top = self.canvas.yview()[0] * self.total_h
        bottom = top + ch
        margin = ch * 0.25
        now = time.monotonic()
        if self._last_viewport_top >= 0 and abs(top - self._last_viewport_top) > 1:
            self._last_viewport_move = now
        self._last_viewport_top = top
        visible = self._visible_tiles(top - margin, bottom + margin)
        want = {t.fi for t in visible}
        for fi in [fi for fi in self.drawn if fi not in want]:
            self._drop_tile(fi)
        wanted_keys = []
        for t in visible:
            d = self.drawn.get(t.fi)
            if d is None:
                d = self._create_tile(t)
            elif d.img is None and not d.broken:
                self._try_fill_tile(t, d)
            if d.img is None and not d.broken:
                wanted_keys.append((t.entry.path, t.w))
        # While the view is still moving (wheel animation, scrollbar drag)
        # only placeholders are drawn; decoding is requested once the view
        # has paused, so a long scroll does not queue every screen it passed.
        moving = (now - self._last_viewport_move) * 1000 < SETTLE_MS or self._scroll_target is not None
        if moving and wanted_keys:
            if self._settle_after is not None:
                self.root.after_cancel(self._settle_after)
            self._settle_after = self.root.after(SETTLE_MS, self._settle_sync)
            return
        self.service.begin_viewport(wanted_keys)
        for key in wanted_keys:
            self.service.request(key[0], key[1], priority=0)
        aspects = self.service.aspects
        unknown = [t.entry.path for t in visible if t.entry.path not in aspects]
        if unknown:
            self.service.prioritize_aspects(unknown)
        # Prefetch the next screen in scroll direction (both ways, cheaply).
        pre = PREFETCH_SCREENS * ch
        for t in self._visible_tiles(bottom + margin, bottom + margin + pre):
            if self.service.get(t.entry.path, t.w) is None:
                self.service.request(t.entry.path, t.w, priority=1)
        for t in self._visible_tiles(max(0, top - margin - pre), top - margin):
            if self.service.get(t.entry.path, t.w) is None:
                self.service.request(t.entry.path, t.w, priority=1)

    def _settle_sync(self):
        self._settle_after = None
        self._schedule_sync()

    def _create_tile(self, t: Tile) -> Drawn:
        d = Drawn()
        d.key = (t.entry.path, t.w)
        self.drawn[t.fi] = d
        self._drawn_by_key[d.key] = t.fi
        im = self.service.get(*d.key)
        if im is not None:
            d.img = self.canvas.create_image(t.x, t.y, anchor=tk.NW, image=self._photo(d.key, im),
                                             tags=("grid_item",))
        else:
            if t.entry.path in self.service.failed:
                d.broken = True
            d.ph = self.canvas.create_rectangle(t.x, t.y, t.x + t.w, t.y + t.h,
                                                fill=self.get_color('placeholder'),
                                                outline="", tags=("grid_item",))
            if d.broken:
                self._mark_broken(t, d)
        self._update_tile_outline(t.fi, d)
        return d

    def _mark_broken(self, t: Tile, d: Drawn):
        if d.mark is None:
            d.mark = self.canvas.create_text(t.x + t.w // 2, t.y + t.h // 2, text="✕",
                                             fill=self.get_color('text'), font=(self.font_ui[0], 16),
                                             tags=("grid_item",))

    def _try_fill_tile(self, t: Tile, d: Drawn):
        im = self.service.get(*d.key)
        if im is not None:
            self._fill_tile(t, d, im)

    def _fill_tile(self, t: Tile, d: Drawn, im: Image.Image):
        photo = self._photo(d.key, im)
        if d.ph is not None:
            self.canvas.delete(d.ph)
            d.ph = None
        d.img = self.canvas.create_image(t.x, t.y, anchor=tk.NW, image=photo, tags=("grid_item",))
        # Outlines must stay above the image.
        if d.sel is not None:
            self.canvas.tag_raise(d.sel)
        if d.ring is not None:
            for item in d.ring:
                self.canvas.tag_raise(item)

    def _update_tile_outline(self, fi: int, d: Drawn):
        t = self.tiles[fi]
        if t.entry.selected:
            if d.sel is None:
                o = self.px(2)
                d.sel = self.canvas.create_rectangle(t.x - o, t.y - o, t.x + t.w + o, t.y + t.h + o,
                                                     outline="#0078d4", width=self.px(3), tags=("grid_item",))
        elif d.sel is not None:
            self.canvas.delete(d.sel)
            d.sel = None
        ring = self.focus_highlight_path == t.entry.path
        if ring and d.ring is None:
            # A thick red ring with a white inner edge reads on any photo;
            # _blink_focus_ring flashes it so the eye finds it at once.
            o = self.px(5)
            outer = self.canvas.create_rectangle(t.x - o, t.y - o, t.x + t.w + o, t.y + t.h + o,
                                                 outline="#FF2D2D", width=self.px(8), tags=("grid_item", "focus_ring"))
            inner = self.canvas.create_rectangle(t.x - 1, t.y - 1, t.x + t.w + 1, t.y + t.h + 1,
                                                 outline="#FFFFFF", width=self.px(2), tags=("grid_item", "focus_ring"))
            d.ring = (outer, inner)
        elif not ring and d.ring is not None:
            for item in d.ring:
                self.canvas.delete(item)
            d.ring = None

    def _drop_tile(self, fi: int):
        d = self.drawn.pop(fi, None)
        if d is None:
            return
        for item in (d.img, d.ph, d.sel, d.mark):
            if item is not None:
                self.canvas.delete(item)
        if d.ring is not None:
            for item in d.ring:
                self.canvas.delete(item)
        self._drawn_by_key.pop(d.key, None)

    def _on_thumb_results(self, results):
        touched = False
        for path, width, im in results:
            fi = self._drawn_by_key.get((path, width))
            if fi is None:
                continue
            d = self.drawn.get(fi)
            if d is None or d.img is not None:
                continue
            if im is None:
                d.broken = True
                self._mark_broken(self.tiles[fi], d)
                continue
            t = self.tiles[fi]
            self._fill_tile(t, d, im)
            if abs(im.height - t.h) > 1:
                # Laid out before its ratio was known; the thumbnail now tells us.
                self._aspects_dirty = True
            touched = True
        return touched

    # ----------------------------------------------------------- scrolling

    @staticmethod
    def _wheel_delta(event) -> float:
        num = getattr(event, 'num', None)
        if num == 4:
            return 120.0
        if num == 5:
            return -120.0
        return float(getattr(event, 'delta', 0) or 0)

    def on_wheel(self, event):
        delta = self._wheel_delta(event)
        if delta == 0:
            return
        if self.single_view_mode:
            if delta > 0:
                self.zoom_in_fn()
            else:
                self.zoom_out_fn()
            return
        if not self.grid_mode or self.total_h <= self.canvas.winfo_height():
            return
        notches = delta / 120.0 if abs(delta) >= 120 else delta / 3.0
        self._scroll_by(-notches * self.px(WHEEL_STEP_PX))
        self._update_hover_from_pointer(event)

    def _scroll_by(self, pixels: float):
        """Animated scroll: the target accumulates while the wheel spins and
        the view eases towards it, like a native gallery, rather than jumping
        a fixed step per notch."""
        if self.total_h <= 0:
            return
        ch = self.canvas.winfo_height()
        cur = self.canvas.yview()[0] * self.total_h
        if self._scroll_target is None:
            self._scroll_target = cur
        self._scroll_target = max(0.0, min(self.total_h - ch, self._scroll_target + pixels))
        if self._scroll_anim is None:
            self._scroll_anim = self.root.after(0, self._scroll_step)

    def _scroll_step(self):
        self._scroll_anim = None
        if self._scroll_target is None or self.total_h <= 0 or not self.grid_mode:
            self._scroll_target = None
            return
        cur = self.canvas.yview()[0] * self.total_h
        diff = self._scroll_target - cur
        step = diff * 0.35
        if abs(step) < 1.0:
            # The canvas positions in whole pixels; a sub-pixel step would
            # never land, so snap to the target and finish the animation.
            nxt = self._scroll_target
            self._scroll_target = None
        else:
            nxt = cur + step
        self.canvas.yview_moveto(nxt / self.total_h)
        self._schedule_sync()
        if self._scroll_target is not None:
            self._scroll_anim = self.root.after(16, self._scroll_step)

    def _scroll_grid_to_index(self, fi: int):
        if not self.grid_mode or not (0 <= fi < len(self.tiles)) or self.total_h <= 0:
            return
        t = self.tiles[fi]
        ch = max(self.canvas.winfo_height(), 1)
        target_top = max(0, t.y - (ch - t.h) // 2)
        self._scroll_target = None
        self.canvas.yview_moveto(min(1.0, target_top / self.total_h))
        self._set_focus_highlight(t.entry.path)
        self._schedule_sync()

    def _set_focus_highlight(self, path: str, duration_ms: int = 3500):
        old = self.focus_highlight_path
        self.focus_highlight_path = path
        if self._focus_highlight_after is not None:
            self.root.after_cancel(self._focus_highlight_after)
        for p in (old, path):
            e = self.lib.by_path(p) if p else None
            fi = self._fi_by_index.get(e.index) if e else None
            if fi is not None and fi in self.drawn:
                self._update_tile_outline(fi, self.drawn[fi])
        self._focus_highlight_after = self.root.after(duration_ms, self._clear_focus_highlight)
        self._blink_focus_ring(path, 6)

    def _blink_focus_ring(self, path: str, steps: int):
        """Flash the ring (hidden/shown) a few times so it is impossible to miss."""
        if self.focus_highlight_path != path or steps <= 0:
            try:
                self.canvas.itemconfig("focus_ring", state='normal')
            except tk.TclError:
                pass
            return
        try:
            self.canvas.itemconfig("focus_ring", state='hidden' if steps % 2 == 0 else 'normal')
        except tk.TclError:
            return
        self.root.after(140, lambda: self._blink_focus_ring(path, steps - 1))

    def _clear_focus_highlight(self):
        self._focus_highlight_after = None
        path, self.focus_highlight_path = self.focus_highlight_path, None
        e = self.lib.by_path(path) if path else None
        fi = self._fi_by_index.get(e.index) if e else None
        if fi is not None and fi in self.drawn:
            self._update_tile_outline(fi, self.drawn[fi])

    # ---------------------------------------------------------- mouse/grid

    def on_canvas_motion(self, event):
        if self.grid_mode:
            self._update_hover_tooltip(event.x, event.y)
        else:
            self._hide_hover_tooltip()

    def _update_hover_from_pointer(self, event):
        self._update_hover_tooltip(event.x, event.y)

    def _update_hover_tooltip(self, ex: int, ey: int):
        t = self._tile_at(self.canvas.canvasx(ex), self.canvas.canvasy(ey))
        if t is None:
            self._hide_hover_tooltip()
            return
        name = t.entry.name
        if self.hover_label is None:
            self.hover_label = tk.Label(self.canvas, text=name, bg='#000000', fg='#ffffff',
                                        font=self._tip_font, padx=self.px(6), pady=self.px(3), borderwidth=0)
        elif self.hover_fi != t.fi:
            self.hover_label.config(text=name)
        self.hover_fi = t.fi
        lw = self._tip_font.measure(name) + self.px(12)
        lh = self._tip_font.metrics("linespace") + self.px(6)
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        lx = min(max(ex + self.px(14), 0), max(0, cw - lw))
        ly = min(max(ey + self.px(14), 0), max(0, ch - lh))
        self.hover_label.place(in_=self.canvas, x=lx, y=ly)
        self.hover_label.lift()

    def _hide_hover_tooltip(self):
        if self.hover_label is not None:
            try:
                self.hover_label.place_forget()
            except tk.TclError:
                pass
        self.hover_fi = None

    def on_click(self, event):
        if self.grid_mode:
            self.handle_grid_click(event)
        elif self.single_view_mode and self.zoom_level > 1.0:
            self.pan_start_x, self.pan_start_y = event.x, event.y

    def on_drag(self, event):
        if self.single_view_mode and self.zoom_level > 1.0 and self._single_item is not None:
            dx, dy = event.x - self.pan_start_x, event.y - self.pan_start_y
            self.pan_start_x, self.pan_start_y = event.x, event.y
            self.pan_x += dx
            self.pan_y += dy
            self.canvas.move(self._single_item, dx, dy)
            self._schedule_single_render(40)

    def on_release(self, event):
        if self.single_view_mode and self.zoom_level > 1.0:
            self._schedule_single_render(0)

    def handle_grid_click(self, event):
        t = self._tile_at(self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))
        if t is None:
            return
        ctrl_held = bool(event.state & 0x0004)
        shift_held = bool(event.state & 0x0001)
        fi = t.fi
        if ctrl_held or shift_held:
            if shift_held and self.last_clicked_idx is not None and 0 <= self.last_clicked_idx < len(self.filtered):
                self._select_range_fi(self.last_clicked_idx, fi)
            else:
                self.lib.toggle(t.entry.index)
            self.last_clicked_idx = fi
            self._apply_selection_change()
            return
        self.last_clicked_idx = fi
        self._focus_tree_on_index(fi)

    def on_double_click(self, event):
        if not self.grid_mode:
            return
        t = self._tile_at(self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))
        if t is None:
            return
        self.last_clicked_idx = t.fi
        self.saved_scroll_pos = self.canvas.yview()[0]
        self.show_single_img(t.fi)

    # ============================================================ single view

    def show_single_img(self, fi: int):
        if not (0 <= fi < len(self.filtered)):
            return
        self.current_index = fi
        self.single_view_mode = True
        self.grid_mode = False
        self.zoom_level = 1.0
        self.zoom_val.config(text="100%")
        self.pan_x = self.pan_y = 0
        self._hide_hover_tooltip()
        self._clear_drawn()
        if self._empty_text_id is not None:
            self.canvas.delete(self._empty_text_id)
            self._empty_text_id = None
        self.canvas.config(scrollregion=(0, 0, 1, 1))
        self.canvas.yview_moveto(0)
        self.update_ui_state()
        self._load_current()
        self._focus_tree_on_index(fi)

    def _leave_single_view(self):
        """Tear down the single view completely (image item, decoded source,
        zoom) so nothing of it stays on the canvas under the grid."""
        self.single_view_mode = False
        self.grid_mode = True
        self.zoom_level = 1.0
        self.pan_x = self.pan_y = 0
        self._view_seq += 1
        self._single_src = None
        self._single_entry = None
        if self._single_item is not None:
            self.canvas.delete(self._single_item)
            self._single_item = None
        self._single_photo = None
        self._single_last = None

    def back_to_grid(self):
        self._leave_single_view()
        self.update_ui_state()
        self._relayout(keep_anchor=False)
        if self.total_h > 0:
            self.canvas.yview_moveto(self.saved_scroll_pos)
        self._sync_viewport()
        if 0 <= self.current_index < len(self.filtered):
            self._focus_tree_on_index(self.current_index)

    def prev_img(self):
        if self.filtered and self.current_index > 0:
            self._step(-1)

    def next_img(self):
        if self.filtered and self.current_index < len(self.filtered) - 1:
            self._step(1)

    def _step(self, delta: int):
        self.current_index += delta
        self.pan_x = self.pan_y = 0
        self.zoom_level = 1.0
        self.zoom_val.config(text="100%")
        self._load_current()
        self._focus_tree_on_index(self.current_index)
        self._update_select_btn_label()

    def zoom_in_fn(self):
        self._set_zoom(min(ZOOM_MAX, self.zoom_level + ZOOM_STEP))

    def zoom_out_fn(self):
        self._set_zoom(max(ZOOM_MIN, self.zoom_level - ZOOM_STEP))

    def _set_zoom(self, z: float):
        if abs(z - self.zoom_level) < 1e-9:
            return
        if z <= 1.0:
            self.pan_x = self.pan_y = 0
        else:
            # Keep the image centre where it was, scaled.
            f = z / self.zoom_level
            self.pan_x = int(self.pan_x * f)
            self.pan_y = int(self.pan_y * f)
        self.zoom_level = z
        self.zoom_val.config(text=f"{int(round(z * 100))}%")
        self._ensure_source_for_zoom()
        self._render_single(force=True)

    def _current_entry(self) -> Optional[ImageEntry]:
        if 0 <= self.current_index < len(self.filtered):
            return self.filtered[self.current_index]
        return None

    def _load_current(self):
        e = self._current_entry()
        if e is None:
            return
        self._single_entry = e
        self._view_seq += 1
        self._single_src = None
        self._single_last = None
        self._set_info(f"{self.current_index + 1} / {len(self.filtered)} - {e.name}")
        dec = self._decoded.get(e.path)
        if dec is None:
            # Instant preview from whatever thumbnail is already in memory.
            prev = self._best_preview(e.path)
            if prev is not None:
                dec = Decoded(prev, prev.size, False)
        if dec is not None:
            self._single_src = dec
            self._render_single(force=True)
        else:
            if self._single_item is not None:
                self.canvas.itemconfig(self._single_item, state='hidden')
        self._ensure_source_for_zoom()
        self._preload_adjacent()

    def _best_preview(self, path: str) -> Optional[Image.Image]:
        for mw in (1024, 512):
            im = self.service.masters.get((path, mw))
            if im is not None:
                return im
        return None

    def _target_size(self, full_size) -> Tuple[int, int]:
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        fw, fh = full_size
        scale = min(cw / fw, ch / fh) * self.zoom_level
        return max(1, int(fw * scale)), max(1, int(fh * scale))

    def _ensure_source_for_zoom(self):
        """Ask the decode worker for a larger source when the screen needs
        more pixels than the one we hold."""
        e = self._current_entry()
        if e is None:
            return
        src = self._single_src
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        if src is not None and (src.is_full or self._src_sufficient(src)):
            return
        if src is not None and src.im.size == src.full_size and src.is_full:
            return
        tw, th = self._target_size(src.full_size) if src is not None else (cw, ch)
        self._request_decode(e.path, max(tw, cw), max(th, ch), full=self.zoom_level > 1.0 and src is not None
                             and src.full_size is not None and tw >= src.full_size[0])

    def _src_sufficient(self, src: Decoded) -> bool:
        if src.is_full:
            return True
        tw, th = self._target_size(src.full_size)
        return src.im.width >= tw and src.im.height >= th

    def _request_decode(self, path: str, min_w: int, min_h: int, full: bool = False):
        self._decode_wanted = self._neighbour_paths()
        key = (path, full, 0 if full else min_w)
        if key in self._decode_inflight:
            return
        self._decode_inflight.add(key)
        self._decode_q.put((path, min_w, min_h, full, key))

    def _neighbour_paths(self) -> set:
        out = set()
        for i in (self.current_index - 1, self.current_index, self.current_index + 1):
            if 0 <= i < len(self.filtered):
                out.add(self.filtered[i].path)
        return out

    def _preload_adjacent(self):
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        for i in (self.current_index + 1, self.current_index - 1):
            if 0 <= i < len(self.filtered):
                p = self.filtered[i].path
                if self._decoded.get(p) is None:
                    self._request_decode(p, cw, ch)
        # Re-queue the current image last so the LIFO worker takes it first.
        e = self._current_entry()
        if e is not None and self._single_src is not None and not self._src_sufficient(self._single_src):
            self._request_decode(e.path, cw, ch)

    def _decode_worker(self):
        while True:
            path, min_w, min_h, full, key = self._decode_q.get()
            try:
                if path not in self._decode_wanted:
                    continue
                with Image.open(path) as im:
                    fw, fh = im.size
                    try:
                        orient = im.getexif().get(0x0112, 1)
                    except Exception:
                        orient = 1
                if orient in (5, 6, 7, 8):
                    fw, fh = fh, fw
                if full or (min_w >= fw and min_h >= fh):
                    im = open_full(path)
                else:
                    im = open_scaled(path, min_w, min_h)
                dec = Decoded(im, (fw, fh), im.size == (fw, fh))
                self._decode_results.put((path, dec))
            except Exception as err:
                if os.environ.get('IMAGEFLOW_DEBUG'):
                    print(f"decode failed for {path}: {err!r}", file=sys.stderr)
                self._decode_results.put((path, None))
            finally:
                self._decode_inflight.discard(key)

    def _on_decoded(self, path: str, dec: Optional[Decoded]):
        if dec is None:
            e = self._current_entry()
            if e is not None and e.path == path and self._single_src is None:
                self._set_info("Error loading image")
            return
        old = self._decoded.get(path)
        if old is None or dec.is_full or dec.im.width >= old.im.width:
            self._decoded.put(path, dec)
        e = self._current_entry()
        if self.single_view_mode and e is not None and e.path == path:
            cur = self._single_src
            if cur is None or dec.is_full or dec.im.width >= cur.im.width:
                self._single_src = dec
                self._render_single(force=True)
                self._ensure_source_for_zoom()

    def _schedule_single_render(self, ms: int):
        if self._single_render_after is not None:
            self.root.after_cancel(self._single_render_after)
        self._single_render_after = self.root.after(ms, lambda: self._render_single(force=False))

    def _render_single(self, force: bool = False):
        self._single_render_after = None
        if not self.single_view_mode:
            return
        src = self._single_src
        if src is None:
            return
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        tw, th = self._target_size(src.full_size)
        ox = cw / 2 + self.pan_x - tw / 2
        oy = ch / 2 + self.pan_y - th / 2
        # Visible part of the scaled image, in scaled-image coordinates.
        vx0, vy0 = max(0, int(-ox)), max(0, int(-oy))
        vx1, vy1 = min(tw, int(-ox + cw) + 1), min(th, int(-oy + ch) + 1)
        last = getattr(self, '_single_last', None)
        if not force and last is not None and self._single_item is not None:
            lsrc, lz, lcw, lch, x0, y0, x1, y1 = last
            if lsrc is src and lz == self.zoom_level and (lcw, lch) == (cw, ch) \
                    and x0 <= vx0 and y0 <= vy0 and x1 >= vx1 and y1 >= vy1:
                self.canvas.coords(self._single_item, ox + x0, oy + y0)
                return
        # Render margin around the viewport so small pans need no re-render.
        # Tighter when zoomed far in, where each rendered pixel costs the most.
        frac = 0.5 if self.zoom_level <= 2.0 else 0.3
        mx, my = cw * frac, ch * frac
        x0, y0 = max(0, int(-ox - mx)), max(0, int(-oy - my))
        x1, y1 = min(tw, int(-ox + cw + mx) + 1), min(th, int(-oy + ch + my) + 1)
        if x1 <= x0 or y1 <= y0:
            if self._single_item is not None:
                self.canvas.itemconfig(self._single_item, state='hidden')
            return
        sw, sh = src.im.size
        sx, sy = sw / tw, sh / th
        box = (x0 * sx, y0 * sy, x1 * sx, y1 * sy)
        out = (x1 - x0, y1 - y0)
        if (x0, y0, x1, y1) == (0, 0, tw, th) and out == (sw, sh):
            im = src.im
        elif out[0] < sw:
            im = src.im.resize(out, Image.Resampling.LANCZOS, box=box, reducing_gap=2.0)
        else:
            im = src.im.resize(out, Image.Resampling.BILINEAR, box=box)
        photo = ImageTk.PhotoImage(im)
        self._single_photo = photo
        px, py = ox + x0, oy + y0
        if self._single_item is None:
            self._single_item = self.canvas.create_image(px, py, anchor=tk.NW, image=photo)
            self.canvas.tag_raise('ghost')
        else:
            self.canvas.coords(self._single_item, px, py)
            self.canvas.itemconfig(self._single_item, image=photo, state='normal')
        self._single_last = (src, self.zoom_level, cw, ch, x0, y0, x1, y1)

    # ============================================================ focus mode

    def on_escape(self, event=None):
        if self.fullscreen_mode and self.single_view_mode:
            self.back_to_grid()
            return
        if self.fullscreen_mode:
            self.exit_fullscreen()
            return
        if self.single_view_mode:
            if self.view_mode.get() == "selected" and len(self.filtered) == 1:
                self.view_mode.set("all")
            else:
                self.back_to_grid()

    def toggle_fullscreen(self):
        if self.fullscreen_mode:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def _set_titlebar_visible(self, visible: bool):
        if IS_WIN:
            try:
                import ctypes
                GWL_STYLE, WS_CAPTION, WS_SYSMENU, SWP_FLAGS = -16, 0x00C00000, 0x00080000, 0x0027
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
                style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
                style = (style | WS_CAPTION | WS_SYSMENU) if visible else (style & ~(WS_CAPTION | WS_SYSMENU))
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style)
                ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)
                return
            except Exception:
                pass
        try:
            self.root.overrideredirect(not visible)
        except tk.TclError:
            pass

    def enter_fullscreen(self):
        self.fullscreen_mode = True
        self.toolbar.pack_forget()
        if self.sidebar_visible:
            self._hide_sidebar_pane()
        self.nav_frame.pack_forget()
        self.full_btn.config(text="Exit Focus")
        self._set_titlebar_visible(False)
        self._show_focus_controls()
        if not IS_WIN:
            self._enable_edge_resize()

    def exit_fullscreen(self):
        self.fullscreen_mode = False
        self._hide_focus_exit_btn()
        if not IS_WIN:
            self._disable_edge_resize()
        self._set_titlebar_visible(True)
        self.toolbar.pack(side=tk.TOP, fill=tk.X, before=self.main)
        if self.sidebar_visible:
            self._show_sidebar_pane()
        self.nav_frame.pack(fill=tk.X, padx=self.px(8), pady=self.px(6))
        self.full_btn.config(text="Focus")

    def toggle_true_fullscreen(self):
        self.true_fullscreen = not self.true_fullscreen
        try:
            self.root.attributes('-fullscreen', self.true_fullscreen)
        except tk.TclError:
            self.true_fullscreen = False

    # ----- edge-drag resize (non-Windows borderless mode) ---------------------

    def _enable_edge_resize(self):
        self._resize_motion_id = self.root.bind("<Motion>", self._on_resize_motion, add="+")
        self._resize_press_id = self.root.bind("<ButtonPress-1>", self._on_resize_press, add="+")
        self._resize_drag_id = self.root.bind("<B1-Motion>", self._on_resize_drag, add="+")
        self._resize_release_id = self.root.bind("<ButtonRelease-1>", self._on_resize_release, add="+")
        self._resize_edge = self._resize_active = self._resize_start = None

    def _disable_edge_resize(self):
        for attr, evt in (('_resize_motion_id', '<Motion>'), ('_resize_press_id', '<ButtonPress-1>'),
                          ('_resize_drag_id', '<B1-Motion>'), ('_resize_release_id', '<ButtonRelease-1>')):
            bid = getattr(self, attr, None)
            if bid:
                try:
                    self.root.unbind(evt, bid)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        try:
            self.root.configure(cursor="")
        except tk.TclError:
            pass
        self._resize_edge = self._resize_active = None

    def _edge_at(self, x_root, y_root):
        try:
            wx, wy = self.root.winfo_rootx(), self.root.winfo_rooty()
            ww, wh = self.root.winfo_width(), self.root.winfo_height()
        except tk.TclError:
            return None
        e = self.EDGE_PX
        rx, ry = x_root - wx, y_root - wy
        if rx < -e or ry < -e or rx > ww + e or ry > wh + e:
            return None
        on_left, on_right = rx <= e, rx >= ww - e
        on_top, on_bottom = ry <= e, ry >= wh - e
        if on_top and on_left:
            return 'nw'
        if on_top and on_right:
            return 'ne'
        if on_bottom and on_left:
            return 'sw'
        if on_bottom and on_right:
            return 'se'
        if on_top:
            return 'n'
        if on_bottom:
            return 's'
        if on_left:
            return 'w'
        if on_right:
            return 'e'
        return None

    def _on_resize_motion(self, event):
        if not self.fullscreen_mode or self._resize_active:
            return
        edge = self._edge_at(event.x_root, event.y_root)
        if edge == self._resize_edge:
            return
        self._resize_edge = edge
        try:
            self.root.configure(cursor=self._CURSOR_FOR_EDGE.get(edge, ""))
        except tk.TclError:
            pass

    def _on_resize_press(self, event):
        if not self.fullscreen_mode:
            return
        edge = self._edge_at(event.x_root, event.y_root)
        if not edge:
            return
        try:
            self._resize_start = (event.x_root, event.y_root, self.root.winfo_rootx(), self.root.winfo_rooty(),
                                  self.root.winfo_width(), self.root.winfo_height())
        except tk.TclError:
            return
        self._resize_active = edge

    def _on_resize_drag(self, event):
        if not self._resize_active or not self._resize_start:
            return
        sx, sy, wx, wy, ww, wh = self._resize_start
        dx, dy = event.x_root - sx, event.y_root - sy
        edge = self._resize_active
        new_x, new_y, new_w, new_h = wx, wy, ww, wh
        if 'e' in edge:
            new_w = max(self.MIN_W, ww + dx)
        if 'w' in edge:
            new_w = max(self.MIN_W, ww - dx)
            new_x = wx + (ww - new_w)
        if 's' in edge:
            new_h = max(self.MIN_H, wh + dy)
        if 'n' in edge:
            new_h = max(self.MIN_H, wh - dy)
            new_y = wy + (wh - new_h)
        try:
            self.root.geometry(f"{new_w}x{new_h}+{new_x}+{new_y}")
        except tk.TclError:
            pass

    def _on_resize_release(self, _event):
        self._resize_active = self._resize_start = None

    # ----- ghost controls -------------------------------------------------------
    # Drawn as canvas text, not widgets: a widget always paints a solid box,
    # which showed as a dark rectangle over light photos. Text items have no
    # background; a one-pixel dark shadow keeps the glyph readable anywhere.

    GHOSTS = {
        'prev': ("‹", 36, 'w'),
        'next': ("›", 36, 'e'),
        'exit': ("✕", 18, 'ne'),
    }

    def _show_focus_controls(self):
        if not self._ghost_items:
            for key, (glyph, size, anchor) in self.GHOSTS.items():
                font = (self.font_ui[0], size)
                shadow = self.canvas.create_text(0, 0, text=glyph, font=font, fill='#000000',
                                                 anchor=anchor, state='hidden', tags=('ghost', f'ghost_{key}'))
                glyph_id = self.canvas.create_text(0, 0, text=glyph, font=font, fill=self._ghost_dim_fg(),
                                                   anchor=anchor, state='hidden', tags=('ghost', f'ghost_{key}'))
                self._ghost_items[key] = (shadow, glyph_id)
                cmd = {'prev': self.prev_img, 'next': self.next_img, 'exit': self.exit_fullscreen}[key]
                self.canvas.tag_bind(f'ghost_{key}', '<Button-1>', lambda e, c=cmd: (c(), 'break')[1])
                self.canvas.tag_bind(f'ghost_{key}', '<Enter>', lambda e, k=key: self._ghost_hover(k, True))
                self.canvas.tag_bind(f'ghost_{key}', '<Leave>', lambda e, k=key: self._ghost_hover(k, False))
        self._place_ghosts()
        self._restyle_focus_controls()
        self._hide_all_focus_ghosts()
        self._focus_motion_bind = self.canvas.bind("<Motion>", self._on_focus_motion, add="+")
        self._focus_leave_bind = self.canvas.bind("<Leave>", lambda e: self._hide_all_focus_ghosts(), add="+")

    def _place_ghosts(self):
        if not self._ghost_items:
            return
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        m = self.px(18)
        pos = {'prev': (m, ch // 2), 'next': (cw - m, ch // 2), 'exit': (cw - m, m)}
        for key, (shadow, glyph) in self._ghost_items.items():
            x, y = pos[key]
            self.canvas.coords(shadow, x + 1, y + 1)
            self.canvas.coords(glyph, x, y)
        self.canvas.tag_raise('ghost')

    def _ghost_hover(self, key, on):
        items = self._ghost_items.get(key)
        if items:
            self.canvas.itemconfig(items[1], fill='#ffffff' if on else self._ghost_dim_fg())

    def _ghost_dim_fg(self):
        return '#e6e6e6'

    def _restyle_focus_controls(self):
        for shadow, glyph in self._ghost_items.values():
            self.canvas.itemconfig(glyph, fill=self._ghost_dim_fg())

    def _on_focus_motion(self, event):
        if not self.fullscreen_mode or not self.single_view_mode:
            return
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        self._set_ghost_visible('prev', event.x < cw * 0.20)
        self._set_ghost_visible('next', event.x > cw * 0.80)
        self._set_ghost_visible('exit', event.y < ch * 0.15)

    def _set_ghost_visible(self, key, want_visible, side=None):
        items = self._ghost_items.get(key)
        if items is None or self._focus_ctrl_visible.get(key, False) == want_visible:
            return
        state = 'normal' if want_visible else 'hidden'
        for item in items:
            self.canvas.itemconfig(item, state=state)
        if want_visible:
            self.canvas.tag_raise('ghost')
            self._fade_ghost_in(key)
        self._focus_ctrl_visible[key] = want_visible

    def _fade_ghost_in(self, key):
        items = self._ghost_items.get(key)
        if not items:
            return
        steps = ['#606060', '#909090', '#b8b8b8', '#d0d0d0', self._ghost_dim_fg()]

        def step(i=0):
            if i >= len(steps) or not self._focus_ctrl_visible.get(key):
                return
            try:
                self.canvas.itemconfig(items[1], fill=steps[i])
            except tk.TclError:
                return
            self.root.after(35, lambda: step(i + 1))
        step(0)

    def _hide_all_focus_ghosts(self):
        for key in ('prev', 'next', 'exit'):
            self._set_ghost_visible(key, False)

    def _hide_focus_exit_btn(self):
        self._hide_all_focus_ghosts()
        for attr, evt in (('_focus_motion_bind', '<Motion>'), ('_focus_leave_bind', '<Leave>')):
            bid = getattr(self, attr)
            if bid:
                try:
                    self.canvas.unbind(evt, bid)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        # Unbinding with an id removes every <Motion> handler on the canvas in
        # some Tk builds; restore the hover tooltip handler.
        self.canvas.bind("<Motion>", self.on_canvas_motion)
        self.canvas.bind("<Leave>", lambda e: self._hide_hover_tooltip())
        self._focus_ctrl_visible = {'prev': False, 'next': False, 'exit': False}

    # ================================================================== web

    def _requested_port(self) -> Optional[int]:
        raw = (self.port_var.get() or '').strip()
        try:
            port = int(raw)
        except ValueError:
            self._set_info("Port must be a number between 1024 and 65535")
            return None
        if not (1 <= port <= 65535):
            self._set_info("Port must be between 1 and 65535")
            return None
        from .web import browser_safe_port
        safe = browser_safe_port(port)
        self._port_note = ""
        if safe != port:
            # e.g. 6000 is the X11 port: Chrome, Edge and Firefox refuse to open it.
            self._port_note = f"Port {port} is blocked by browsers (ERR_UNSAFE_PORT), so "
            self.port_var.set(str(safe))
            port = safe
        return port

    def open_web(self):
        port = self._requested_port()
        if port is None:
            return
        if self.web_server is not None and self.web_server.port != port and self.web_server.requested_port != port:
            # The user typed a different port: move the server there.
            try:
                self.web_server.shutdown()
            except Exception:
                pass
            self.web_server = None
        if self.web_server is None:
            try:
                from .web import start_server
                self.web_server = start_server(self.lib, self.service, self.web_host, port,
                                               allow_edit=bool(getattr(self.cli_args, 'web_edit', False)))
                self.web_server.requested_port = port
            except Exception as err:
                hint = " (ports below 1024 need administrator rights)" if port < 1024 else ""
                self._set_info(f"Web view could not start on port {port}{hint}: {err}")
                return
        self.web_port = self.web_server.port
        note = getattr(self, '_port_note', '')
        if self.web_server.port != port:
            self.port_var.set(str(self.web_server.port))
            self._set_info(f"{note}port {port} is busy; web view is on {self.web_server.url}")
        elif note:
            self._set_info(f"{note}the web view is on {self.web_server.url}")
        else:
            self._set_info(f"Web view: {self.web_server.url}")
        try:
            webbrowser.open(self.web_server.url)
        except Exception:
            pass

    # ================================================================= pump

    def _pump(self):
        """Single periodic task on the Tk thread: applies results from worker
        threads and changes made through the web viewer. It must never die,
        so every step is guarded."""
        busy = False
        try:
            busy = self._pump_once()
        except Exception as err:  # pragma: no cover - defensive
            if os.environ.get('IMAGEFLOW_DEBUG'):
                import traceback
                traceback.print_exc()
        self.root.after(16 if (busy or self.loading) else 120, self._pump)

    def _pump_once(self) -> bool:
        busy = False
        try:
            while True:
                token, folder, entries, err = self._scan_results.get_nowait()
                if token == self._scan_token:
                    self._finish_scan(folder, entries, err)
        except queue.Empty:
            pass

        results = self.service.drain(96)
        if results and self.grid_mode:
            self._on_thumb_results(results)
        busy = bool(results) or self.service.pending_count() > 0

        if self._aspects_dirty:
            now = time.monotonic()
            if now - self._last_aspect_relayout >= ASPECT_RELAYOUT_INTERVAL:
                self._aspects_dirty = False
                self._last_aspect_relayout = now
                if self.grid_mode and self.tiles:
                    self._relayout(keep_anchor=True)
            else:
                busy = True

        try:
            while True:
                path, dec = self._decode_results.get_nowait()
                self._on_decoded(path, dec)
        except queue.Empty:
            pass
        busy = busy or bool(self._decode_inflight)

        if self.lib.scan_id != self._seen_scan_id:
            self._seen_scan_id = self.lib.scan_id
            self._seen_version = self.lib.version
            self.service.clear_memory()
            self._photos.clear()
            self.lib.pop_dirty()
            self._after_library_replaced(self.lib.entries)
        elif self.lib.version != self._seen_version:
            self._seen_version = self.lib.version
            dirty = self.lib.pop_dirty()
            if dirty:
                if self.view_mode.get() == "selected":
                    self.update_view()
                else:
                    self._refresh_dirty(dirty)
        return busy

    # ================================================================ close

    def on_close(self):
        self._save_settings()
        if self.lib.folder:
            try:
                self.service.save_aspect_index(self.lib.folder, self.lib.entries)
            except Exception:
                pass
        self.service.stop()
        if self.web_server is not None:
            try:
                self.web_server.shutdown()
            except Exception:
                pass
        self._set_titlebar_visible(True)
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            os._exit(0)


def _dpi_aware():
    if IS_WIN:
        try:
            import ctypes
            try:
                # System DPI aware: crisp text, and Tk's own scaling reads the
                # correct DPI so point fonts and px() agree.
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def run(cli_args=None, library: Optional[Library] = None, service: Optional[ThumbnailService] = None,
        web_host: str = '127.0.0.1', web_port: int = 6001, web_server=None) -> ImageGallery:
    """Create the window and run the Tk main loop."""
    _dpi_aware()
    root = tk.Tk()
    app = ImageGallery(root, library=library, service=service, cli_args=cli_args,
                       web_host=web_host, web_port=web_port,
                       ui_scale=getattr(cli_args, 'scale', None))
    if web_server is not None:
        app.web_server = web_server
        web_server.requested_port = web_port
        app.web_port = web_server.port
        app.port_var.set(str(web_server.port))
        app._set_info(f"Web view: {web_server.url}")
    root.mainloop()
    return app
