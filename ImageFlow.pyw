import tkinter as tk
from tkinter import filedialog, ttk
from PIL import Image, ImageTk
import os
from collections import OrderedDict
import threading
from natsort import natsorted
import queue
import sys
import argparse

class FastImageCache:
    def __init__(self, max_size=100):
        self.cache = OrderedDict()
        self.max_size = max_size
        self.lock = threading.Lock()
    
    def get(self, key):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
        return None
    
    def put(self, key, value):
        with self.lock:
            self.cache[key] = value
            if len(self.cache) > self.max_size:
                self.cache.popitem(last=False)

class ImageGallery:
    def __init__(self, root, cli_args=None):
        self.root = root
        self.root.title("Gallery Viewer")
        self.root.geometry("1200x800")
        
        # CLI arguments
        self.cli_args = cli_args
        
        # Core data
        self.images = []
        self.filtered_images = []
        self.current_index = 0
        self.image_folder = ""
        self.loading = False
        
        # View state
        self.zoom_level = 1.0
        self.sidebar_visible = True
        self.dark_mode = True
        self.fullscreen_mode = False
        self.grid_mode = True
        self.single_view_mode = False
        self.saved_scroll_pos = 0.0
        
        # Pan state
        self.pan_x = 0
        self.pan_y = 0
        self.pan_start_x = 0
        self.pan_start_y = 0
        
        # Canvas image tracking for smooth updates
        self.canvas_image_id = None
        
        # Ghost arrow state
        self.ghost_left_arrow = None
        self.ghost_right_arrow = None
        self.arrow_fade_timer = None
        
        # Optimized caching — thumbs large (cheap), full images small (RAM)
        self.thumb_cache = FastImageCache(max_size=500)
        self.img_cache = FastImageCache(max_size=4)
        self.aspect_cache = {}
        self.photos = []
        
        # Layout
        self.base_width = 200
        self.gap = 4
        self.num_columns = 4
        
        # Async operations
        self.thumb_queue = queue.Queue(maxsize=200)
        self.result_queue = queue.Queue()
        self.stop_workers = False
        
        # Render optimization
        self.render_id = None
        self.image_positions = {}
        self.last_clicked_idx = None
        self.aspect_worker_running = False
        self.hover_label = None
        self.hover_path = None
        self.focus_exit_btn = None
        self.focus_prev_btn = None
        self.focus_next_btn = None
        self._focus_motion_bind = None
        self._focus_leave_bind = None
        self._focus_ctrl_visible = {'prev': False, 'next': False, 'exit': False}
        self.focus_highlight_path = None
        self._focus_highlight_after = None
        self.true_fullscreen = False
        self.tree_populate_token = 0
        
        # Colors — accent (primary action) and chrome (toolbar/sidebar).
        self.colors = {
            'light': {'bg': '#ffffff', 'sidebar': '#f3f3f3', 'header': '#fafafa',
                      'text': '#1a1a1a', 'button': '#2563eb', 'btn_txt': '#ffffff',
                      'btn_hover': '#1d4ed8',
                      'subtle_btn': '#fafafa',           # blends into header
                      'subtle_btn_hover': '#e6e6e6',
                      'subtle_btn_txt': '#1a1a1a',
                      'border': '#dcdcdc'},
            'dark':  {'bg': '#181818', 'sidebar': '#1f1f1f', 'header': '#242424',
                      'text': '#e8e8e8', 'button': '#2563eb', 'btn_txt': '#ffffff',
                      'btn_hover': '#3b82f6',
                      'subtle_btn': '#242424',           # blends into header
                      'subtle_btn_hover': '#333333',
                      'subtle_btn_txt': '#e8e8e8',
                      'border': '#2e2e2e'}
        }
        # Use Segoe UI on Windows for cleaner type; fall back gracefully.
        self.font_ui = ("Segoe UI", 10)
        self.font_ui_bold = ("Segoe UI", 10, "bold")
        self.font_ui_lg = ("Segoe UI", 11)
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.build_ui()
        self.apply_theme()
        
        # Start workers
        for _ in range(4):
            t = threading.Thread(target=self.thumbnail_worker, daemon=True)
            t.start()
        
        # Bindings
        self.root.bind("<Escape>", self.on_escape)
        self.root.bind("<F11>", lambda e: self.toggle_true_fullscreen())
        self.root.bind("<Left>", lambda e: self.prev_img() if self.single_view_mode else None)
        self.root.bind("<Right>", lambda e: self.next_img() if self.single_view_mode else None)
        self.root.bind("<s>", lambda e: self.toggle_current_selection() if self.single_view_mode else None)
        self.root.bind("<S>", lambda e: self.toggle_current_selection() if self.single_view_mode else None)
        self.root.bind("<space>", lambda e: self.toggle_current_selection() if self.single_view_mode else None)
        
        self.check_results()
        
        # Process CLI arguments
        if self.cli_args:
            self.root.after(100, self.process_cli_args)

    def process_cli_args(self):
        """Process command line arguments"""
        if self.cli_args.image:
            image_path = os.path.abspath(self.cli_args.image)
            if os.path.isfile(image_path):
                folder_path = os.path.dirname(image_path)
                self.image_folder = folder_path
                self.load_images_async()
                self.root.after(500, lambda: self.jump_to_image(image_path))
        elif self.cli_args.folder:
            folder_path = os.path.abspath(self.cli_args.folder)
            if os.path.isdir(folder_path):
                self.image_folder = folder_path
                self.load_images_async()
                if self.cli_args.txt:
                    self.root.after(500, lambda: self.select_from_txt(self.cli_args.txt))
        elif self.cli_args.txt:
            txt_path = os.path.abspath(self.cli_args.txt)
            if os.path.isfile(txt_path):
                folder_path = os.path.dirname(txt_path)
                self.image_folder = folder_path
                self.load_images_async()
                self.root.after(500, lambda: self.select_from_txt(self.cli_args.txt))

    def jump_to_image(self, image_path):
        """Jump to a specific image and show it"""
        for idx, img in enumerate(self.filtered_images):
            if img['path'] == image_path:
                self.show_single_img(img)
                return

    def select_from_txt(self, txt_file):
        """Select images from text file"""
        txt_path = os.path.abspath(txt_file)
        if not os.path.isfile(txt_path):
            return
        try:
            with open(txt_path, 'r', encoding='utf-8') as f:
                content = f.read()
            image_names = set()
            if ',' in content:
                parts = content.split(',')
                for part in parts:
                    name = part.strip()
                    if name:
                        image_names.add(name)
            else:
                lines = content.split('\n')
                for line in lines:
                    name = line.strip()
                    if name:
                        image_names.add(name)
            selected_count = 0
            for img in self.images:
                img_name = os.path.basename(img['path'])
                if img_name in image_names:
                    img['selected'] = True
                    selected_count += 1
            for item in self.tree.get_children():
                values = self.tree.item(item, 'values')
                idx = int(values[0]) - 1
                if 0 <= idx < len(self.filtered_images):
                    img_data = self.filtered_images[idx]
                    sel = "✓" if img_data['selected'] else ""
                    self.tree.item(item, values=(values[0], sel, values[2]))
            if selected_count > 0:
                self.view_mode.set("selected")
                self.update_view()
        except Exception as e:
            pass

    def thumbnail_worker(self):
        while not self.stop_workers:
            try:
                task = self.thumb_queue.get(timeout=0.5)
                if task is None:
                    break
                
                path, width, priority = task
                key = f"{path}_{width}"
                
                if self.thumb_cache.get(key):
                    continue
                
                try:
                    with Image.open(path) as img:
                        # draft() lets JPEG decoder load at reduced resolution — huge speedup
                        try:
                            img.draft('RGB', (width * 2, width * 2))
                        except Exception:
                            pass
                        aspect = img.width / img.height
                        new_h = max(1, int(width / aspect))
                        thumb = img.resize((width, new_h), Image.Resampling.BILINEAR)
                    # Opportunistically cache aspect — avoids a second header read on USB
                    self.aspect_cache.setdefault(path, aspect)
                    self.result_queue.put((key, thumb, path))
                except:
                    pass
                
                self.thumb_queue.task_done()
            except queue.Empty:
                continue

    def check_results(self):
        try:
            count = 0
            while count < 20:
                try:
                    key, thumb, path = self.result_queue.get_nowait()
                    self.thumb_cache.put(key, thumb)
                    count += 1
                except queue.Empty:
                    break

            if count > 0 and self.grid_mode:
                self.update_visible_thumbs()
        except:
            pass

        self.root.after(80, self.check_results)

    def get_aspect_ratio_fast(self, path):
        # Never block the UI thread reading image headers — the background
        # _precompute_aspects worker fills this cache. Return a sane default
        # until the real ratio is available; layout will be re-rendered.
        return self.aspect_cache.get(path, 1.0)

    def queue_thumbnail(self, path, width, priority=5):
        try:
            self.thumb_queue.put_nowait((path, width, priority))
        except queue.Full:
            pass

    def get_thumbnail(self, path, width):
        key = f"{path}_{width}"
        return self.thumb_cache.get(key)

    def get_image(self, path):
        cached = self.img_cache.get(path)
        if cached:
            return cached
        
        try:
            img = Image.open(path)
            img.load()  # Force decode and release file handle before caching
            self.img_cache.put(path, img)
            return img
        except:
            return None

    def preload_adjacent(self):
        if not self.single_view_mode or not self.filtered_images:
            return
        
        for offset in [-1, 1]:
            idx = self.current_index + offset
            if 0 <= idx < len(self.filtered_images):
                path = self.filtered_images[idx]['path']
                threading.Thread(target=self._preload_single, args=(path,), daemon=True).start()

    def _preload_single(self, path):
        if not self.img_cache.get(path):
            try:
                img = Image.open(path)
                img.load()
                self.img_cache.put(path, img)
            except:
                pass

    def on_escape(self, event):
        # Cascade: each Esc steps back one layer.
        #   focus + single  → grid (still focus)
        #   focus + grid    → exit focus
        #   single (no focus) → grid
        if self.fullscreen_mode and self.single_view_mode:
            self.back_to_grid()
            return
        if self.fullscreen_mode:
            self.exit_fullscreen()
            return
        if self.single_view_mode:
            if self.view_mode.get() == "selected" and len(self.filtered_images) == 1:
                self.view_mode.set("all")
                self.update_view()
            else:
                self.back_to_grid()

    def on_close(self):
        self.stop_workers = True
        for _ in range(4):
            try:
                self.thumb_queue.put(None, block=False)
            except:
                pass
        # Restore decorations so a stuck borderless state doesn't leak.
        try:
            self.root.overrideredirect(False)
        except tk.TclError:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except:
            os._exit(0)

    def get_color(self, key):
        return self.colors['dark' if self.dark_mode else 'light'][key]

    def build_ui(self):
        self.main = tk.Frame(self.root)
        self.main.pack(fill=tk.BOTH, expand=True)
        
        # Toolbar — slimmer, lighter; primary action gets the accent.
        self.toolbar = tk.Frame(self.root, height=44)
        self.toolbar.pack(side=tk.TOP, fill=tk.X, before=self.main)
        self.toolbar.pack_propagate(False)

        left = tk.Frame(self.toolbar)
        left.pack(side=tk.LEFT, padx=8, pady=6)

        self.select_folder_btn = tk.Button(left, text="📁  Open Folder", command=self.select_folder,
                                           padx=12, pady=6, cursor="hand2", font=self.font_ui)
        self.select_folder_btn.pack(side=tk.LEFT, padx=3)

        self.deselect_btn = tk.Button(left, text="Clear Selection", command=self.deselect_all,
                                      padx=10, pady=6, cursor="hand2", font=self.font_ui,
                                      state=tk.DISABLED)
        self.deselect_btn.pack(side=tk.LEFT, padx=3)

        self.export_btn = tk.Button(left, text="Export", command=self.export_selected,
                                    padx=10, pady=6, cursor="hand2", font=self.font_ui,
                                    state=tk.DISABLED)
        self.export_btn.pack(side=tk.LEFT, padx=3)

        self.back_btn = tk.Button(left, text="◀  Grid", command=self.back_to_grid,
                                  padx=10, pady=6, cursor="hand2", font=self.font_ui)
        self.back_btn.pack(side=tk.LEFT, padx=3)

        right = tk.Frame(self.toolbar)
        right.pack(side=tk.RIGHT, padx=8, pady=6)

        self.full_btn = tk.Button(right, text="Focus", command=self.toggle_fullscreen,
                                  padx=10, pady=6, cursor="hand2", font=self.font_ui)
        self.full_btn.pack(side=tk.RIGHT, padx=3)

        self.dark_btn = tk.Button(right, text="☀" if self.dark_mode else "☾",
                                  command=self.toggle_dark, padx=10, pady=6,
                                  cursor="hand2", font=self.font_ui)
        self.dark_btn.pack(side=tk.RIGHT, padx=3)

        self.side_btn = tk.Button(right, text="Panel", command=self.toggle_sidebar,
                                  padx=10, pady=6, cursor="hand2", font=self.font_ui)
        self.side_btn.pack(side=tk.RIGHT, padx=3)

        # Zoom controls
        self.zoom_txt = tk.Label(right, text="Zoom", font=self.font_ui)
        self.zoom_out = tk.Button(right, text="−", command=self.zoom_out_fn, width=3,
                                  cursor="hand2", pady=4, font=self.font_ui)
        self.zoom_val = tk.Label(right, text="100%", width=5, font=self.font_ui)
        self.zoom_in = tk.Button(right, text="+", command=self.zoom_in_fn, width=3,
                                cursor="hand2", pady=4, font=self.font_ui)
        
        # Sidebar
        self.sidebar = tk.Frame(self.main, width=280)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y, in_=self.main)
        self.sidebar.pack_propagate(False)
        
        # Search
        sf = tk.Frame(self.sidebar)
        sf.pack(fill=tk.X, padx=12, pady=(12, 8))
        tk.Label(sf, text="Search", font=self.font_ui_bold).pack(anchor=tk.W, pady=(0, 4))
        self.search_var = tk.StringVar()
        self.search_var.trace('w', lambda *a: self.update_view())
        tk.Entry(sf, textvariable=self.search_var, font=self.font_ui,
                 relief=tk.FLAT, borderwidth=1).pack(fill=tk.X, ipady=4)

        # View mode
        mf = tk.Frame(self.sidebar)
        mf.pack(fill=tk.X, padx=12, pady=8)
        tk.Label(mf, text="View", font=self.font_ui_bold).pack(anchor=tk.W, pady=(0, 4))

        self.view_mode = tk.StringVar(value="all")
        self.view_mode.trace('w', self.on_view_mode_change)
        tk.Radiobutton(mf, text="All", variable=self.view_mode, value="all",
                       command=self.update_view, cursor="hand2",
                       font=self.font_ui).pack(anchor=tk.W, pady=1)
        tk.Radiobutton(mf, text="Selected Only", variable=self.view_mode, value="selected",
                       command=self.update_view, cursor="hand2",
                       font=self.font_ui).pack(anchor=tk.W, pady=1)

        # Export options
        ef = tk.Frame(self.sidebar)
        ef.pack(fill=tk.X, padx=12, pady=(4, 4))
        tk.Label(ef, text="Export", font=self.font_ui_bold).pack(anchor=tk.W, pady=(0, 4))
        self.export_with_ext = tk.BooleanVar(value=False)
        tk.Checkbutton(ef, text="Include File Extensions",
                       variable=self.export_with_ext, cursor="hand2",
                       font=self.font_ui).pack(anchor=tk.W, pady=1)

        # Columns
        cf = tk.Frame(self.sidebar)
        cf.pack(fill=tk.X, padx=12, pady=8)
        tk.Label(cf, text="Columns", font=self.font_ui_bold).pack(anchor=tk.W, pady=(0, 4))

        col_frame = tk.Frame(cf)
        col_frame.pack(fill=tk.X)

        self.col_var = tk.IntVar(value=4)
        self.col_radios = {}
        for i in [3, 4, 5, 6]:
            rb = tk.Radiobutton(col_frame, text=str(i), variable=self.col_var, value=i,
                                command=self.on_column_change, cursor="hand2",
                                font=self.font_ui)
            rb.pack(side=tk.LEFT, padx=4)
            self.col_radios[i] = rb

        # Image list
        tk.Label(self.sidebar, text="Files", font=self.font_ui_bold).pack(padx=12, pady=(8, 4), anchor=tk.W)
        
        lf = tk.Frame(self.sidebar)
        lf.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        scroll = tk.Scrollbar(lf)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.tree = ttk.Treeview(lf, columns=('num', 'sel', 'name'), show='tree headings',
                                yscrollcommand=scroll.set, height=15)
        scroll.config(command=self.tree.yview)
        
        self.tree.column('#0', width=0, stretch=tk.NO)
        self.tree.column('num', width=40, anchor=tk.CENTER, minwidth=40)
        self.tree.column('sel', width=40, anchor=tk.CENTER, minwidth=40)
        self.tree.column('name', width=180, anchor=tk.W, minwidth=100)
        
        self.tree.heading('num', text='#', anchor=tk.CENTER)
        self.tree.heading('sel', text='✓', anchor=tk.CENTER)
        self.tree.heading('name', text='Filename', anchor=tk.W)
        
        self.tree.pack(fill=tk.BOTH, expand=True)
        
        self.tree.bind('<ButtonRelease-1>', self.on_tree_click)
        self.tree.bind('<Double-1>', self.on_tree_dbl)
        
        # Display area
        self.display = tk.Frame(self.main)
        self.display.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, in_=self.main)
        
        canvas_frame = tk.Frame(self.display)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        self.scrollbar = tk.Scrollbar(canvas_frame, orient=tk.VERTICAL)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.canvas = tk.Canvas(canvas_frame, highlightthickness=0, cursor="hand2",
                               yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.config(command=self.canvas.yview)
        
        self.canvas.bind("<ButtonPress-1>", self.on_click)
        self.canvas.bind("<Double-Button-1>", self.on_double_click)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Button-4>", self.on_wheel)
        self.canvas.bind("<Button-5>", self.on_wheel)
        self.canvas.bind("<Configure>", lambda e: self.schedule_render())
        self.canvas.bind("<Motion>", self.on_canvas_motion)
        self.canvas.bind("<Leave>", lambda e: self._hide_hover_tooltip())
        
        # Navigation
        self.nav_frame = tk.Frame(self.display, height=44)
        self.nav_frame.pack(fill=tk.X, padx=8, pady=6)
        self.nav_frame.pack_propagate(False)
        
        self.prev_btn = tk.Button(self.nav_frame, text="◀  Previous", command=self.prev_img,
                                  padx=14, pady=6, cursor="hand2", font=self.font_ui)
        self.prev_btn.pack(side=tk.LEFT, padx=4)

        self.select_current_btn = tk.Button(self.nav_frame, text="☆  Select  (S)",
                                            command=self.toggle_current_selection,
                                            padx=12, pady=6, cursor="hand2", font=self.font_ui)

        self.info = tk.Label(self.nav_frame, text="Open a folder to begin", font=self.font_ui)
        self.info.pack(side=tk.LEFT, expand=True)

        self.sel_counter = tk.Label(self.nav_frame, text="", font=self.font_ui)
        self.sel_counter.pack(side=tk.RIGHT, padx=8)

        self.next_btn = tk.Button(self.nav_frame, text="Next  ▶", command=self.next_img,
                                  padx=14, pady=6, cursor="hand2", font=self.font_ui)
        self.next_btn.pack(side=tk.RIGHT, padx=4)
        
        self.update_ui_state()


    def on_canvas_motion(self, event):
        """Hover tooltip in grid mode only — ghost arrows removed."""
        if self.grid_mode:
            self._update_hover_tooltip(event)
        else:
            self._hide_hover_tooltip()
        # Defensive: if any leftover ghost items exist, kill them.
        if self.ghost_left_arrow or self.ghost_right_arrow:
            self.hide_ghost_arrows()

    def _update_hover_tooltip(self, event):
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        hover_path = None
        for path, (x, y, w, h) in self.image_positions.items():
            if x <= cx <= x + w and y <= cy <= y + h:
                hover_path = path
                break
        if hover_path is None:
            self._hide_hover_tooltip()
            return

        name = os.path.basename(hover_path)
        if self.hover_label is None:
            self.hover_label = tk.Label(
                self.canvas, text=name,
                bg='#000000', fg='#ffffff', font=('Sans', 10),
                padx=6, pady=3, borderwidth=0)
        elif self.hover_path != hover_path:
            self.hover_label.config(text=name)
        self.hover_path = hover_path

        # Place near cursor; clamp inside the canvas so it doesn't clip.
        self.hover_label.update_idletasks()
        lw = self.hover_label.winfo_reqwidth()
        lh = self.hover_label.winfo_reqheight()
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        lx = min(max(event.x + 14, 0), max(0, cw - lw))
        ly = min(max(event.y + 14, 0), max(0, ch - lh))
        self.hover_label.place(in_=self.canvas, x=lx, y=ly)
        self.hover_label.lift()

    def _hide_hover_tooltip(self):
        if self.hover_label is not None:
            try:
                self.hover_label.place_forget()
            except tk.TclError:
                pass
        self.hover_path = None

    def show_ghost_arrows(self, show_left, show_right, canvas_width, canvas_height):
        """Display rectangle ghost navigation arrows"""
        center_y = canvas_height // 2
        
        if show_left and not self.ghost_left_arrow:
            arrow_x = 50
            arrow_y = center_y
            self.ghost_left_arrow = []
            
            # Rectangle button background
            rect = self.canvas.create_rectangle(
                arrow_x - 35, arrow_y - 50,
                arrow_x + 35, arrow_y + 50,
                fill='#0078d4', outline='#005a9e', width=3, stipple='gray50'
            )
            self.ghost_left_arrow.append(rect)
            
            # Left arrow polygon
            arrow = self.canvas.create_polygon(
                arrow_x + 15, arrow_y - 30,
                arrow_x - 15, arrow_y,
                arrow_x + 15, arrow_y + 30,
                fill='#ffffff', outline='', smooth=False
            )
            self.ghost_left_arrow.append(arrow)
            
            for item in self.ghost_left_arrow:
                self.canvas.tag_bind(item, '<Button-1>', lambda e: self.prev_img())
        
        if show_right and not self.ghost_right_arrow:
            arrow_x = canvas_width - 50
            arrow_y = center_y
            self.ghost_right_arrow = []
            
            # Rectangle button background
            rect = self.canvas.create_rectangle(
                arrow_x - 35, arrow_y - 50,
                arrow_x + 35, arrow_y + 50,
                fill='#0078d4', outline='#005a9e', width=3, stipple='gray50'
            )
            self.ghost_right_arrow.append(rect)
            
            # Right arrow polygon
            arrow = self.canvas.create_polygon(
                arrow_x - 15, arrow_y - 30,
                arrow_x + 15, arrow_y,
                arrow_x - 15, arrow_y + 30,
                fill='#ffffff', outline='', smooth=False
            )
            self.ghost_right_arrow.append(arrow)
            
            for item in self.ghost_right_arrow:
                self.canvas.tag_bind(item, '<Button-1>', lambda e: self.next_img())

    def hide_ghost_arrows(self):
        """Hide ghost arrows"""
        if self.ghost_left_arrow:
            for item in self.ghost_left_arrow:
                self.canvas.delete(item)
            self.ghost_left_arrow = None
        if self.ghost_right_arrow:
            for item in self.ghost_right_arrow:
                self.canvas.delete(item)
            self.ghost_right_arrow = None

    def update_ui_state(self):
        if self.single_view_mode:
            self.back_btn.pack(side=tk.LEFT, padx=5)
            self.zoom_txt.pack(side=tk.RIGHT, padx=5)
            self.zoom_out.pack(side=tk.RIGHT, padx=2)
            self.zoom_val.pack(side=tk.RIGHT, padx=2)
            self.zoom_in.pack(side=tk.RIGHT, padx=2)

            if self.view_mode.get() == "selected" and len(self.filtered_images) == 1:
                self.back_btn.pack_forget()

            if len(self.filtered_images) > 1:
                self.prev_btn.pack(side=tk.LEFT, padx=5)
                self.next_btn.pack(side=tk.RIGHT, padx=5)
            else:
                self.prev_btn.pack_forget()
                self.next_btn.pack_forget()

            # Select button visible only in single view
            self.select_current_btn.pack(side=tk.LEFT, padx=5)
            self._update_select_btn_label()
        else:
            self.back_btn.pack_forget()
            self.zoom_txt.pack_forget()
            self.zoom_out.pack_forget()
            self.zoom_val.pack_forget()
            self.zoom_in.pack_forget()
            self.prev_btn.pack_forget()
            self.next_btn.pack_forget()
            self.select_current_btn.pack_forget()

        any_selected = any(img['selected'] for img in self.images)
        self.deselect_btn.config(state=tk.NORMAL if any_selected else tk.DISABLED)
        self.export_btn.config(state=tk.NORMAL if any_selected else tk.DISABLED)
        self._update_selection_counter()

    def _update_select_btn_label(self):
        if not self.single_view_mode or not self.filtered_images:
            return
        if not (0 <= self.current_index < len(self.filtered_images)):
            return
        picked = self.filtered_images[self.current_index]['selected']
        self.select_current_btn.config(text="★ Selected (S)" if picked else "☆ Select (S)")

    def toggle_current_selection(self):
        if not self.single_view_mode or not self.filtered_images:
            return
        if not (0 <= self.current_index < len(self.filtered_images)):
            return
        img = self.filtered_images[self.current_index]
        img['selected'] = not img['selected']
        self.last_clicked_idx = self.current_index
        self._apply_selection_change()

    def on_view_mode_change(self, *args):
        if self.view_mode.get() == "selected":
            selected_count = sum(1 for img in self.images if img['selected'])
            if selected_count in [1, 2, 3]:
                for radio in self.col_radios.values():
                    radio.config(state=tk.DISABLED)
                self.col_var.set(0)
            else:
                for radio in self.col_radios.values():
                    radio.config(state=tk.NORMAL)
                if selected_count >= 4:
                    self.col_var.set(4)
                    self.num_columns = 4
        else:
            for radio in self.col_radios.values():
                radio.config(state=tk.NORMAL)
            if self.col_var.get() == 0:
                self.col_var.set(4)
                self.num_columns = 4
        
        self.update_ui_state()

    def on_column_change(self):
        self.num_columns = self.col_var.get()
        self.render()

    def schedule_render(self):
        if self.render_id:
            self.root.after_cancel(self.render_id)
        self.render_id = self.root.after(50, self.render)

    def on_wheel(self, event):
        delta = 0
        if hasattr(event, 'delta'):
            delta = event.delta
        elif event.num == 4:
            delta = 120
        elif event.num == 5:
            delta = -120
            
        if self.single_view_mode:
            if delta > 0:
                self.zoom_in_fn()
            else:
                self.zoom_out_fn()
        else:
            self.canvas.yview_scroll(int(-1 * (delta / 120)), "units")
            # Scroll doesn't change layout; render_grid self-cleans now.
            if self.grid_mode:
                self.render_grid()

    def on_click(self, event):
        if self.grid_mode:
            self.handle_grid_click(event)
        elif self.single_view_mode and self.zoom_level > 1.0:
            self.pan_start_x = event.x
            self.pan_start_y = event.y

    def on_drag(self, event):
        if self.single_view_mode and self.zoom_level > 1.0:
            self.pan_x += event.x - self.pan_start_x
            self.pan_y += event.y - self.pan_start_y
            self.pan_start_x = event.x
            self.pan_start_y = event.y
            self.render()

    def handle_grid_click(self, event):
        click_x = self.canvas.canvasx(event.x)
        click_y = self.canvas.canvasy(event.y)

        ctrl_held = bool(event.state & 0x0004)
        shift_held = bool(event.state & 0x0001)

        for path, (x, y, w, h) in self.image_positions.items():
            if x <= click_x <= x + w and y <= click_y <= y + h:
                img_data = next((img for img in self.filtered_images if img['path'] == path), None)
                if not img_data:
                    return
                idx = self.filtered_images.index(img_data)
                if ctrl_held or shift_held:
                    if shift_held and self.last_clicked_idx is not None \
                            and 0 <= self.last_clicked_idx < len(self.filtered_images):
                        # Range SELECT (file-explorer style) — never deselects
                        start = min(self.last_clicked_idx, idx)
                        end = max(self.last_clicked_idx, idx)
                        for i in range(start, end + 1):
                            self.filtered_images[i]['selected'] = True
                    else:
                        # Ctrl+click: toggle this specific image only
                        img_data['selected'] = not img_data['selected']
                    self.last_clicked_idx = idx
                    self._apply_selection_change()
                    return
                # Plain click: highlight matching row in the sidebar tree
                # (filename only — no tick toggle). Double-click opens single
                # view; the tick column is unaffected by either action.
                self.last_clicked_idx = idx
                self._focus_tree_on_index(idx)
                return

    def on_double_click(self, event):
        if not self.grid_mode:
            return
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        for path, (x, y, w, h) in self.image_positions.items():
            if x <= cx <= x + w and y <= cy <= y + h:
                img_data = next((img for img in self.filtered_images if img['path'] == path), None)
                if not img_data:
                    return
                self.last_clicked_idx = self.filtered_images.index(img_data)
                self.saved_scroll_pos = self.canvas.yview()[0]
                self.show_single_img(img_data)
                return

    def show_single_img(self, img_data):
        if img_data in self.filtered_images:
            self.canvas_image_id = None
            self.current_index = self.filtered_images.index(img_data)
            self.single_view_mode = True
            self.grid_mode = False
            self.zoom_level = 1.0
            self.zoom_val.config(text="100%")
            self.pan_x = self.pan_y = 0
            self.update_ui_state()
            self.display_current_image()
            self._focus_tree_on_index(self.current_index)

    def back_to_grid(self):
        self.single_view_mode = False
        self.grid_mode = True
        self.zoom_level = 1.0
        self.pan_x = self.pan_y = 0
        self.canvas_image_id = None
        self.update_ui_state()

        self.canvas.delete("all")
        self.photos.clear()
        # Recompute scrollregion first, flush so the canvas applies it,
        # THEN restore scroll position, THEN render the visible window.
        # Previously yview_moveto ran before the scrollregion was active and
        # the grid always opened at the top.
        self.calculate_layout()
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(self.saved_scroll_pos)
        self.canvas.update_idletasks()
        self.render_grid()
        if 0 <= self.current_index < len(self.filtered_images):
            self._focus_tree_on_index(self.current_index)

    def zoom_in_fn(self):
        self.zoom_level = min(5.0, self.zoom_level + 0.25)
        self.zoom_val.config(text=f"{int(self.zoom_level * 100)}%")
        self.render()

    def zoom_out_fn(self):
        self.zoom_level = max(0.25, self.zoom_level - 0.25)
        self.zoom_val.config(text=f"{int(self.zoom_level * 100)}%")
        if self.zoom_level <= 1.0:
            self.pan_x = 0
            self.pan_y = 0
        self.render()
    
    def apply_theme(self):
        bg = self.get_color('bg')
        sb = self.get_color('sidebar')
        hd = self.get_color('header')
        tx = self.get_color('text')
        btn = self.get_color('button')
        btn_txt = self.get_color('btn_txt')
        
        self.root.configure(bg=bg)
        self.main.configure(bg=bg)
        self.toolbar.configure(bg=hd)
        self.sidebar.configure(bg=sb)
        self.display.configure(bg=bg)
        self.canvas.configure(bg=bg)
        self.nav_frame.configure(bg=bg)
        self.info.configure(bg=bg, fg=tx)
        
        # Toolbar uses subtle buttons — chrome blends into the header, the
        # blue accent is reserved for primary actions in the nav bar.
        sub_bg = self.get_color('subtle_btn')
        sub_hover = self.get_color('subtle_btn_hover')
        sub_fg = self.get_color('subtle_btn_txt')
        accent_hover = self.get_color('btn_hover')

        for child in self.toolbar.winfo_children():
            try:
                child.configure(bg=hd)
            except:
                pass
            for btn_child in child.winfo_children():
                try:
                    if isinstance(btn_child, tk.Button):
                        self._style_btn(btn_child, sub_bg, sub_hover, sub_fg)
                    elif isinstance(btn_child, tk.Label):
                        btn_child.configure(bg=hd, fg=tx)
                except:
                    pass

        self._apply_sidebar_theme(sb, tx)

        self._style_btn(self.prev_btn, btn, accent_hover, btn_txt)
        self._style_btn(self.next_btn, btn, accent_hover, btn_txt)
        self._style_btn(self.select_current_btn, btn, accent_hover, btn_txt)
        self.sel_counter.configure(bg=bg, fg=tx)
        
        style = ttk.Style()
        if self.dark_mode:
            style.theme_use('default')
            style.configure("Treeview", background="#1f1f1f", foreground="#e8e8e8",
                          fieldbackground="#1f1f1f", rowheight=25, borderwidth=0)
            style.map('Treeview', background=[('selected', '#2563eb')])
            style.configure("Treeview.Heading", background="#242424", foreground="#e8e8e8", relief="flat")
        else:
            style.theme_use('default')
            style.configure("Treeview", background="#ffffff", foreground="#000000",
                          fieldbackground="#ffffff", rowheight=25, borderwidth=0)
            style.map('Treeview', background=[('selected', '#0078d4')])
            style.configure("Treeview.Heading", background="#e8e8e8", foreground="#000000", relief="flat")

    def _style_btn(self, btn, base_bg, hover_bg, fg):
        """Apply a flat, hover-aware look. Replaces any prior Enter/Leave
        bindings so theme switches don't stack handlers."""
        try:
            btn.configure(bg=base_bg, fg=fg,
                          activebackground=hover_bg, activeforeground=fg,
                          relief=tk.FLAT, borderwidth=0, highlightthickness=0)
        except tk.TclError:
            return
        btn._base_bg = base_bg
        btn._hover_bg = hover_bg

        def on_enter(_e, b=btn, hb=hover_bg):
            if str(b.cget('state')) != tk.DISABLED:
                b.configure(bg=hb)

        def on_leave(_e, b=btn, bb=base_bg):
            b.configure(bg=bb)

        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)

    def _apply_sidebar_theme(self, bg, fg):
        for child in self.sidebar.winfo_children():
            self._apply_theme_recursive(child, bg, fg)

    def _apply_theme_recursive(self, widget, bg, fg):
        try:
            if isinstance(widget, (tk.Frame, tk.Label)):
                if isinstance(widget, tk.Label):
                    widget.configure(bg=bg, fg=fg)
                else:
                    widget.configure(bg=bg)
            elif isinstance(widget, tk.Entry):
                entry_bg = '#2a2a2a' if self.dark_mode else '#ffffff'
                entry_fg = '#e8e8e8' if self.dark_mode else '#000000'
                widget.configure(bg=entry_bg, fg=entry_fg, insertbackground=entry_fg)
            elif isinstance(widget, tk.Radiobutton):
                widget.configure(bg=bg, fg=fg, selectcolor=bg, activebackground=bg, activeforeground=fg)
            elif isinstance(widget, tk.Checkbutton):
                widget.configure(bg=bg, fg=fg, selectcolor=bg, activebackground=bg, activeforeground=fg)
            elif isinstance(widget, tk.Button):
                btn_bg = self.get_color('button')
                btn_fg = self.get_color('btn_txt')
                self._style_btn(widget, btn_bg, self.get_color('btn_hover'), btn_fg)
        except:
            pass
        
        for child in widget.winfo_children():
            self._apply_theme_recursive(child, bg, fg)

    def toggle_dark(self):
        self.dark_mode = not self.dark_mode
        self.dark_btn.config(text="☀" if self.dark_mode else "☾")
        self.apply_theme()
        self.render()

    def toggle_sidebar(self):
        if self.sidebar_visible:
            self.sidebar.pack_forget()
            self.side_btn.config(text="Panel")
        else:
            self.sidebar.pack(side=tk.LEFT, fill=tk.Y, in_=self.main, before=self.display)
            self.side_btn.config(text="Panel")
        self.sidebar_visible = not self.sidebar_visible
        self.render()

    def toggle_fullscreen(self):
        if self.fullscreen_mode:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self):
        """Borderless Focus mode — hides ALL chrome (toolbar, sidebar, nav,
        and the OS title bar) while keeping the current window size. Press
        Esc to step back (single→grid, then exit focus). F11 toggles real
        OS fullscreen independently. Window edges remain drag-resizable."""
        self.fullscreen_mode = True
        self.toolbar.pack_forget()
        if self.sidebar_visible:
            self.sidebar.pack_forget()
        self.nav_frame.pack_forget()
        self.full_btn.config(text="Exit Focus")
        # Drop the title bar / min-max-close. Wrap because some WMs reject it.
        try:
            self.root.overrideredirect(True)
        except tk.TclError:
            pass
        self._show_focus_controls()
        self._enable_edge_resize()
        self.render()

    def exit_fullscreen(self):
        self.fullscreen_mode = False
        self._hide_focus_exit_btn()
        self._disable_edge_resize()
        try:
            if self.root.overrideredirect():
                self.root.overrideredirect(False)
        except tk.TclError:
            pass
        self.toolbar.pack(side=tk.TOP, fill=tk.X, before=self.main)
        if self.sidebar_visible:
            self.sidebar.pack(side=tk.LEFT, fill=tk.Y, in_=self.main, before=self.display)
        self.nav_frame.pack(fill=tk.X, padx=8, pady=6)
        self.full_btn.config(text="Focus")
        self.hide_ghost_arrows()
        self.render()

    # ----- Edge-drag resize for borderless focus mode -----------------------
    # overrideredirect(True) strips the OS title bar AND the OS resize handles.
    # We re-implement resize ourselves: when the cursor is within EDGE_PX of a
    # window edge in focus mode, change the cursor and let the user drag to
    # resize. Works on all 4 sides + 4 corners.
    EDGE_PX = 8
    MIN_W = 320
    MIN_H = 240

    def _enable_edge_resize(self):
        # Bind on root so we catch motion regardless of which child has focus.
        self._resize_motion_id = self.root.bind(
            "<Motion>", self._on_resize_motion, add="+")
        self._resize_press_id = self.root.bind(
            "<ButtonPress-1>", self._on_resize_press, add="+")
        self._resize_drag_id = self.root.bind(
            "<B1-Motion>", self._on_resize_drag, add="+")
        self._resize_release_id = self.root.bind(
            "<ButtonRelease-1>", self._on_resize_release, add="+")
        self._resize_edge = None       # which edge cursor is on, e.g. 'nw','e'
        self._resize_active = None     # edge currently being dragged
        self._resize_start = None      # (mouse_x_root, mouse_y_root, win_x, win_y, w, h)

    def _disable_edge_resize(self):
        for attr, evt in (('_resize_motion_id', '<Motion>'),
                          ('_resize_press_id', '<ButtonPress-1>'),
                          ('_resize_drag_id', '<B1-Motion>'),
                          ('_resize_release_id', '<ButtonRelease-1>')):
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
        self._resize_edge = None
        self._resize_active = None

    def _edge_at(self, x_root, y_root):
        """Return which edge/corner the root-coord point is on, or None."""
        try:
            wx = self.root.winfo_rootx()
            wy = self.root.winfo_rooty()
            ww = self.root.winfo_width()
            wh = self.root.winfo_height()
        except tk.TclError:
            return None
        e = self.EDGE_PX
        rx = x_root - wx
        ry = y_root - wy
        if rx < -e or ry < -e or rx > ww + e or ry > wh + e:
            return None
        on_left   = rx <= e
        on_right  = rx >= ww - e
        on_top    = ry <= e
        on_bottom = ry >= wh - e
        if on_top and on_left:     return 'nw'
        if on_top and on_right:    return 'ne'
        if on_bottom and on_left:  return 'sw'
        if on_bottom and on_right: return 'se'
        if on_top:    return 'n'
        if on_bottom: return 's'
        if on_left:   return 'w'
        if on_right:  return 'e'
        return None

    _CURSOR_FOR_EDGE = {
        'n': 'sb_v_double_arrow', 's': 'sb_v_double_arrow',
        'e': 'sb_h_double_arrow', 'w': 'sb_h_double_arrow',
        'nw': 'size_nw_se', 'se': 'size_nw_se',
        'ne': 'size_ne_sw', 'sw': 'size_ne_sw',
    }

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
            self._resize_start = (
                event.x_root, event.y_root,
                self.root.winfo_rootx(), self.root.winfo_rooty(),
                self.root.winfo_width(), self.root.winfo_height(),
            )
        except tk.TclError:
            return
        self._resize_active = edge

    def _on_resize_drag(self, event):
        if not self._resize_active or not self._resize_start:
            return
        sx, sy, wx, wy, ww, wh = self._resize_start
        dx = event.x_root - sx
        dy = event.y_root - sy
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
        self._resize_active = None
        self._resize_start = None

    def toggle_true_fullscreen(self):
        """F11 — real OS fullscreen, independent of Focus mode."""
        self.true_fullscreen = not self.true_fullscreen
        try:
            self.root.attributes('-fullscreen', self.true_fullscreen)
        except tk.TclError:
            self.true_fullscreen = False

    def _show_focus_controls(self):
        """Ghost edge controls — prev/next arrows hide by default and fade
        in only when the mouse approaches the left/right edges. The exit ✕
        sits in the top-right and reveals on top-edge hover. Mouse motion
        on the canvas drives visibility so the image stays unobstructed."""
        bg = self.get_color('bg')
        if not hasattr(self, 'focus_prev_btn') or self.focus_prev_btn is None \
                or not self.focus_prev_btn.winfo_exists():
            self.focus_prev_btn = tk.Label(
                self.canvas, text="‹", font=("Segoe UI", 36),
                bg=bg, padx=16, pady=8, cursor="hand2")
            self.focus_next_btn = tk.Label(
                self.canvas, text="›", font=("Segoe UI", 36),
                bg=bg, padx=16, pady=8, cursor="hand2")
            self.focus_exit_btn = tk.Label(
                self.canvas, text="✕", font=("Segoe UI", 18),
                bg=bg, padx=14, pady=6, cursor="hand2")
            self.focus_prev_btn.bind("<Button-1>", lambda e: self.prev_img())
            self.focus_next_btn.bind("<Button-1>", lambda e: self.next_img())
            self.focus_exit_btn.bind("<Button-1>", lambda e: self.exit_fullscreen())
            for w in (self.focus_prev_btn, self.focus_next_btn, self.focus_exit_btn):
                self._bind_ghost_hover(w)

        self._restyle_focus_controls()
        # Place at edges but keep them hidden until motion brings them in.
        self.focus_prev_btn.place_forget()
        self.focus_next_btn.place_forget()
        self.focus_exit_btn.place_forget()
        self._focus_ctrl_visible = {'prev': False, 'next': False, 'exit': False}
        # Bind motion on the canvas so we can reveal/hide based on mouse zone.
        self._focus_motion_bind = self.canvas.bind(
            "<Motion>", self._on_focus_motion, add="+")
        self._focus_leave_bind = self.canvas.bind(
            "<Leave>", lambda e: self._hide_all_focus_ghosts(), add="+")

    def _bind_ghost_hover(self, w):
        def enter(_e, _w=w):
            try:
                _w.configure(fg=self.get_color('btn_txt'))
            except tk.TclError:
                pass
        def leave(_e, _w=w):
            try:
                _w.configure(fg=self._ghost_dim_fg())
            except tk.TclError:
                pass
        w.bind("<Enter>", enter)
        w.bind("<Leave>", leave)

    def _ghost_dim_fg(self):
        return '#c0c0c0' if self.dark_mode else '#404040'

    def _restyle_focus_controls(self):
        bg = self.get_color('bg')
        dim = self._ghost_dim_fg()
        for w in (getattr(self, 'focus_prev_btn', None),
                  getattr(self, 'focus_next_btn', None),
                  getattr(self, 'focus_exit_btn', None)):
            if w is None:
                continue
            try:
                w.configure(bg=bg, fg=dim)
            except tk.TclError:
                pass

    def _on_focus_motion(self, event):
        """Reveal an arrow when the mouse enters the corresponding edge zone,
        hide it when the mouse leaves. Edge zones are 20% of canvas width
        (arrows) and the top 15% of canvas height (exit)."""
        if not self.fullscreen_mode or not self.single_view_mode:
            return
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        x, y = event.x, event.y

        in_left   = x < cw * 0.20
        in_right  = x > cw * 0.80
        in_top    = y < ch * 0.15

        self._set_ghost_visible('prev', in_left,  side='left')
        self._set_ghost_visible('next', in_right, side='right')
        self._set_ghost_visible('exit', in_top,   side='top-right')

    def _set_ghost_visible(self, key, want_visible, side):
        widget = {
            'prev': getattr(self, 'focus_prev_btn', None),
            'next': getattr(self, 'focus_next_btn', None),
            'exit': getattr(self, 'focus_exit_btn', None),
        }.get(key)
        if widget is None:
            return
        currently = self._focus_ctrl_visible.get(key, False)
        if want_visible == currently:
            return
        if want_visible:
            try:
                if side == 'left':
                    widget.place(in_=self.canvas, relx=0.0, rely=0.5,
                                 anchor='w', x=14)
                elif side == 'right':
                    widget.place(in_=self.canvas, relx=1.0, rely=0.5,
                                 anchor='e', x=-14)
                elif side == 'top-right':
                    widget.place(in_=self.canvas, relx=1.0, rely=0.0,
                                 anchor='ne', x=-14, y=14)
                widget.lift()
                self._fade_widget_in(widget)
            except tk.TclError:
                return
        else:
            try:
                widget.place_forget()
            except tk.TclError:
                pass
        self._focus_ctrl_visible[key] = want_visible

    def _fade_widget_in(self, widget):
        """Color-ramp fade from near-bg to dim, simulating alpha animation."""
        steps = (['#202020', '#404040', '#707070', '#a0a0a0', self._ghost_dim_fg()]
                 if self.dark_mode
                 else ['#e8e8e8', '#bbbbbb', '#888888', '#606060', self._ghost_dim_fg()])
        def step(i=0):
            if i >= len(steps) or not widget.winfo_exists():
                return
            try:
                widget.configure(fg=steps[i])
            except tk.TclError:
                return
            self.root.after(35, lambda: step(i + 1))
        step(0)

    def _hide_all_focus_ghosts(self):
        for key in ('prev', 'next', 'exit'):
            self._set_ghost_visible(key, False, side='left')

    def _hide_focus_exit_btn(self):
        """Tear down all ghost overlays and unbind motion handlers."""
        for attr in ('focus_prev_btn', 'focus_next_btn', 'focus_exit_btn'):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.place_forget()
                except tk.TclError:
                    pass
        if hasattr(self, '_focus_motion_bind') and self._focus_motion_bind:
            try:
                self.canvas.unbind("<Motion>", self._focus_motion_bind)
            except tk.TclError:
                pass
            self._focus_motion_bind = None
        if hasattr(self, '_focus_leave_bind') and self._focus_leave_bind:
            try:
                self.canvas.unbind("<Leave>", self._focus_leave_bind)
            except tk.TclError:
                pass
            self._focus_leave_bind = None
        self._focus_ctrl_visible = {'prev': False, 'next': False, 'exit': False}

    def select_folder(self):
        if self.loading:
            return
        folder = filedialog.askdirectory(title="Select Image Folder")
        if folder:
            self.image_folder = folder
            self.load_images_async()

    def load_images_async(self):
        self.loading = True
        self.info.config(text="Scanning...")
        self.root.update_idletasks()
        threading.Thread(target=self._scan_folder_thread, daemon=True).start()

    def _scan_folder_thread(self):
        exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tiff'}
        files = []
        
        try:
            with os.scandir(self.image_folder) as entries:
                for entry in entries:
                    if entry.is_file():
                        ext = os.path.splitext(entry.name)[1].lower()
                        if ext in exts:
                            files.append({'name': entry.name, 'path': entry.path, 'selected': False})
        except Exception as e:
            self.root.after(0, lambda: self.info.config(text=f"Error: {e}"))
            self.loading = False
            return
        
        files = natsorted(files, key=lambda x: x['name'])
        # Precompute aspect ratios for the first chunk SYNCHRONOUSLY (still on
        # this background scan thread) so the initial render shows the proper
        # masonry/collage look instead of all-squares placeholders.
        paths = [f['path'] for f in files]
        INITIAL_BATCH = min(800, len(paths))
        self.root.after(0, lambda: self.info.config(
            text=f"Preparing layout for {len(files)} images..."))
        for path in paths[:INITIAL_BATCH]:
            if self.stop_workers:
                break
            if path in self.aspect_cache:
                continue
            try:
                with Image.open(path) as img:
                    self.aspect_cache[path] = img.width / img.height
            except Exception:
                self.aspect_cache[path] = 1.0
        self.root.after(0, lambda: self._finish_loading(files))
        # Remainder fills in background so 10k+ folders still scroll smoothly.
        if len(paths) > INITIAL_BATCH:
            threading.Thread(target=self._precompute_aspects,
                             args=(paths[INITIAL_BATCH:],), daemon=True).start()

    def _precompute_aspects(self, paths):
        # Background fill for the tail of huge folders. Re-renders periodically
        # so the collage settles into its real shape as ratios become known.
        self.aspect_worker_running = True
        for i, path in enumerate(paths):
            if self.stop_workers:
                break
            if path in self.aspect_cache:
                continue
            try:
                with Image.open(path) as img:
                    self.aspect_cache[path] = img.width / img.height
            except Exception:
                self.aspect_cache[path] = 1.0
            if i and i % 200 == 0 and self.grid_mode:
                self.root.after(0, self.schedule_render)
        self.aspect_worker_running = False
        if self.grid_mode:
            self.root.after(0, self.schedule_render)

    def _finish_loading(self, files):
        self.images = files
        self.filtered_images.clear()
        self.aspect_cache.clear()
        self.image_positions.clear()
        self.last_clicked_idx = None
        self.tree.delete(*self.tree.get_children())

        self.info.config(text=f"Loaded {len(self.images)} images")
        self.loading = False
        self.update_view()

    def update_view(self):
        if self.loading:
            return

        search = self.search_var.get().lower()
        mode = self.view_mode.get()

        self.filtered_images = []

        for img in self.images:
            matches_search = not search or search in img['name'].lower()
            matches_mode = mode != "selected" or img['selected']

            if matches_search and matches_mode:
                self.filtered_images.append(img)

        self._rebuild_tree_chunked()

        if mode == "selected":
            count = len(self.filtered_images)
            
            if count in [1, 2, 3]:
                for radio in self.col_radios.values():
                    radio.config(state=tk.DISABLED)
                self.col_var.set(0)
            else:
                for radio in self.col_radios.values():
                    radio.config(state=tk.NORMAL)
                if count >= 4 and self.col_var.get() == 0:
                    self.col_var.set(4)
                    self.num_columns = 4
            
            if count == 1:
                self.show_single_img(self.filtered_images[0])
                return
        
        self.update_ui_state()
        self.render()

    def _rebuild_tree_chunked(self):
        """Rebuild the sidebar tree in small async chunks so the UI stays
        responsive even with tens of thousands of images on slow storage."""
        self.tree_populate_token += 1
        token = self.tree_populate_token
        self.tree.delete(*self.tree.get_children())
        self._insert_tree_chunk(token, 0, 300)

    def _insert_tree_chunk(self, token, start, chunk):
        if token != self.tree_populate_token:
            return  # Superseded by a newer rebuild
        end = min(start + chunk, len(self.filtered_images))
        for i in range(start, end):
            img = self.filtered_images[i]
            sel = "✓" if img['selected'] else ""
            self.tree.insert('', 'end', values=(str(i + 1), sel, img['name']))
        if end < len(self.filtered_images):
            self.root.after(1, lambda: self._insert_tree_chunk(token, end, chunk))

    def _apply_selection_change(self):
        """Fast path after toggling one or more images. Avoids the full
        update_view tree rebuild (costly with thousands of rows) unless the
        'Selected Only' filter is active and the visible set actually changed.
        """
        if self.view_mode.get() == "selected":
            self.update_view()
            return
        # In 'All Images' mode the filter is unchanged — just refresh ✓ marks
        # for the rows currently in the tree.
        children = self.tree.get_children()
        n = min(len(children), len(self.filtered_images))
        for i in range(n):
            img = self.filtered_images[i]
            values = self.tree.item(children[i], 'values')
            if not values:
                continue
            sel = "✓" if img['selected'] else ""
            if values[1] != sel:
                self.tree.item(children[i], values=(values[0], sel, values[2]))
        self._update_selection_counter()
        self.update_ui_state()
        if self.grid_mode:
            self.render_grid()
        elif self.single_view_mode:
            self._update_select_btn_label()

    def _focus_tree_on_index(self, idx):
        """Highlight + scroll the sidebar tree to row `idx` so the list
        always reflects the user's current image."""
        children = self.tree.get_children()
        if not (0 <= idx < len(children)):
            return
        item = children[idx]
        try:
            self.tree.selection_set(item)
            self.tree.focus(item)
            self.tree.see(item)
        except tk.TclError:
            pass

    def _scroll_grid_to_index(self, idx):
        """Scroll the grid canvas so the image at `idx` is visible. Used when
        clicking a filename in the sidebar — should NEVER toggle selection."""
        if not self.grid_mode or not self.filtered_images:
            return
        if not (0 <= idx < len(self.filtered_images)):
            return
        path = self.filtered_images[idx]['path']
        pos = self.image_positions.get(path)
        if not pos:
            return
        _x, y, _w, h = pos
        sr = self.canvas.cget("scrollregion")
        if not sr:
            return
        try:
            total_h = float(sr.split()[3])
        except (ValueError, IndexError):
            return
        if total_h <= 0:
            return
        canvas_h = max(self.canvas.winfo_height(), 1)
        # Center the image roughly in the viewport
        target_top = max(0, y - (canvas_h - h) // 2)
        frac = min(1.0, target_top / total_h)
        try:
            self.canvas.yview_moveto(frac)
            self._set_focus_highlight(path)
        except tk.TclError:
            pass

    def _set_focus_highlight(self, path, duration_ms=2200):
        """Mark `path` with a bright yellow ring so the user can spot the
        image they just navigated to. Auto-clears after `duration_ms`."""
        self.focus_highlight_path = path
        if self._focus_highlight_after is not None:
            try:
                self.root.after_cancel(self._focus_highlight_after)
            except Exception:
                pass
            self._focus_highlight_after = None
        if self.grid_mode:
            self.render_grid()
        self._focus_highlight_after = self.root.after(
            duration_ms, self._clear_focus_highlight)

    def _clear_focus_highlight(self):
        self.focus_highlight_path = None
        self._focus_highlight_after = None
        if self.grid_mode:
            try:
                self.canvas.delete("focus_ring")
                self.render_grid()
            except tk.TclError:
                pass

    def _update_selection_counter(self):
        if not hasattr(self, 'sel_counter'):
            return
        total = len(self.images)
        picked = sum(1 for img in self.images if img['selected'])
        if total == 0:
            self.sel_counter.config(text="")
        else:
            self.sel_counter.config(text=f"{picked} selected / {total} total")

    def on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        item = self.tree.identify_row(event.y)

        if not item or region != "cell":
            return

        column = self.tree.identify_column(event.x)
        if column not in ('#1', '#2', '#3'):
            return

        values = self.tree.item(item, 'values')
        try:
            idx = int(values[0]) - 1
        except (ValueError, IndexError):
            return
        if not (0 <= idx < len(self.filtered_images)):
            return

        shift_held = bool(event.state & 0x0001)
        ctrl_held = bool(event.state & 0x0004)

        # Two distinct behaviours by column:
        #   ✓ column (#2)  → toggle the tick (with Shift/Ctrl modifiers)
        #   filename / #   → highlight + scroll the grid to that image
        # The filename column never adds a tick — that was the major bug.
        if column == '#2':
            if shift_held and self.last_clicked_idx is not None \
                    and 0 <= self.last_clicked_idx < len(self.filtered_images):
                start = min(self.last_clicked_idx, idx)
                end = max(self.last_clicked_idx, idx)
                for i in range(start, end + 1):
                    self.filtered_images[i]['selected'] = True
            elif ctrl_held:
                self.filtered_images[idx]['selected'] = not self.filtered_images[idx]['selected']
            else:
                self.filtered_images[idx]['selected'] = not self.filtered_images[idx]['selected']
            self.last_clicked_idx = idx
            self._apply_selection_change()
            return

        # Filename / number column — navigation only, no tick.
        self.last_clicked_idx = idx
        self._focus_tree_on_index(idx)
        if self.grid_mode:
            self._scroll_grid_to_index(idx)

    def on_tree_dbl(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            values = self.tree.item(item, 'values')
            if values:
                try:
                    idx = int(values[0]) - 1
                    if 0 <= idx < len(self.filtered_images):
                        self.saved_scroll_pos = self.canvas.yview()[0]
                        self.show_single_img(self.filtered_images[idx])
                except ValueError:
                    pass

    def deselect_all(self):
        for img in self.images:
            img['selected'] = False

        for item in self.tree.get_children():
            values = self.tree.item(item, 'values')
            self.tree.item(item, values=(values[0], "", values[2]))

        self.last_clicked_idx = None
        self.view_mode.set("all")
        self.update_view()
        self._update_selection_counter()

    def export_selected(self):
        """Write selected image filenames (one per line) to a .txt file.
        The 'Include file extensions' checkbox in the sidebar controls
        whether names are written as 'photo.jpg' or 'photo'."""
        selected = [img for img in self.images if img['selected']]
        if not selected:
            self.info.config(text="No selected images to export")
            return
        include_ext = bool(self.export_with_ext.get())
        default_name = "selected_images.txt"
        path = filedialog.asksaveasfilename(
            title="Export Selected Filenames",
            defaultextension=".txt",
            initialfile=default_name,
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                for img in selected:
                    name = img['name']
                    if not include_ext:
                        name = os.path.splitext(name)[0]
                    f.write(name + "\n")
            self.info.config(text=f"Exported {len(selected)} names → {os.path.basename(path)}")
        except Exception as e:
            self.info.config(text=f"Export failed: {e}")

    def render(self):
        if self.single_view_mode:
            self.display_current_image()
            return
        
        self.canvas.delete("all")
        self.photos.clear()
        
        if not self.filtered_images:
            self.canvas.create_text(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                text="No images\nSelect a folder", font=("Sans", 14), fill=self.get_color('text'), justify=tk.CENTER)
            return
        
        self.calculate_layout()
        self.render_grid()

    def calculate_layout(self):
        canvas_width = self.canvas.winfo_width()
        if canvas_width <= 1:
            # Canvas not yet realized — retry the full render (previous version
            # only re-ran calculate_layout, leaving the grid blank on startup)
            self.root.after(100, self.schedule_render)
            return
        
        if self.view_mode.get() == "selected":
            count = len(self.filtered_images)
            if count == 2:
                cols = 2
            elif count == 3:
                cols = 3
            else:
                cols = self.num_columns if self.num_columns > 0 else 4
        else:
            cols = self.num_columns if self.num_columns > 0 else 4
        
        gap = self.gap
        if cols == 0:
            cols = 1
        col_w = (canvas_width - (gap * (cols + 1))) // cols
        col_w = max(50, col_w)
        
        col_heights = [gap] * cols
        self.image_positions.clear()
        
        for img in self.filtered_images:
            path = img['path']
            min_col = col_heights.index(min(col_heights))
            aspect = self.get_aspect_ratio_fast(path)
            img_h = int(col_w / aspect)
            x = gap + min_col * (col_w + gap)
            y = col_heights[min_col]
            self.image_positions[path] = (x, y, col_w, img_h)
            col_heights[min_col] = y + img_h + gap
        
        total_h = max(col_heights) + gap if col_heights else 0
        self.canvas.config(scrollregion=(0, 0, canvas_width, total_h))

    def render_grid(self):
        # Clear any prior grid items first — render_grid is called from many
        # paths (scroll, selection toggle, scroll-to-index, thumb arrival) and
        # without this guard each call stacks fresh photos/rectangles on top
        # of the old ones, which is what produced the broken/duplicated grid.
        self.canvas.delete("grid_item")
        self.photos.clear()

        canvas_h = self.canvas.winfo_height()
        sr = self.canvas.cget("scrollregion")
        if not sr:
            total_h = canvas_h
        else:
            sr_parts = sr.split()
            total_h = int(float(sr_parts[3])) if len(sr_parts) >= 4 else canvas_h

        st_fraction = self.canvas.yview()[0]
        visible_top = st_fraction * total_h - 400
        visible_bottom = visible_top + canvas_h + 800

        focus_path = getattr(self, 'focus_highlight_path', None)

        for path, (x, y, w, h) in self.image_positions.items():
            if y + h < visible_top or y > visible_bottom:
                continue

            thumb = self.get_thumbnail(path, w)

            if thumb:
                try:
                    photo = ImageTk.PhotoImage(thumb)
                    self.photos.append(photo)

                    img_data = next((img for img in self.filtered_images if img['path'] == path), None)
                    if img_data and img_data['selected']:
                        self.canvas.create_rectangle(x-2, y-2, x+w+2, y+h+2,
                            outline="#0078d4", width=3, tags=("grid_item",))

                    self.canvas.create_image(x, y, anchor=tk.NW, image=photo,
                        tags=("grid_item",))

                    # "You clicked this one" — bright yellow ring drawn on
                    # top of the thumbnail. Cleared after a few seconds.
                    if path == focus_path:
                        self.canvas.create_rectangle(x-4, y-4, x+w+4, y+h+4,
                            outline="#F60000", width=7,
                            tags=("grid_item", "focus_ring"))
                except:
                    pass
            else:
                self.canvas.create_rectangle(x, y, x+w, y+h,
                                            fill=self.get_color('sidebar'),
                                            outline=self.get_color('text'),
                                            tags=("grid_item",))
                priority = 10 if (visible_top + 400) <= y <= (visible_bottom - 400) else 5
                self.queue_thumbnail(path, w, priority)

    def update_visible_thumbs(self):
        if self.grid_mode:
            self.render_grid()

    def display_current_image(self):
        if not self.filtered_images or self.current_index >= len(self.filtered_images):
            return
        
        img_data = self.filtered_images[self.current_index]
        path = img_data['path']
        
        self.canvas.config(scrollregion=(0, 0, 1, 1))
        self.canvas.yview_moveto(0)
        
        img = self.get_image(path)
        if not img:
            self.info.config(text="Error loading image")
            return
        
        self.root.update_idletasks()
        cw = max(self.canvas.winfo_width(), 100)
        ch = max(self.canvas.winfo_height(), 100)
        iw, ih = img.size
        
        scale = min(cw / iw, ch / ih) * self.zoom_level
        new_w = max(1, int(iw * scale))
        new_h = max(1, int(ih * scale))
        
        resized = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
        photo = ImageTk.PhotoImage(resized)
        self.photos.clear()
        self.photos.append(photo)
        
        x = cw // 2 + self.pan_x
        y = ch // 2 + self.pan_y
        
        # Update existing image instead of recreating (prevents flicker)
        if self.canvas_image_id is None:
            self.canvas.delete("all")
            self.canvas_image_id = self.canvas.create_image(x, y, anchor=tk.CENTER, image=photo)
        else:
            self.canvas.coords(self.canvas_image_id, x, y)
            self.canvas.itemconfig(self.canvas_image_id, image=photo)
        
        self.info.config(text=f"{self.current_index + 1} / {len(self.filtered_images)} - {img_data['name']}")
        self.preload_adjacent()

    def prev_img(self):
        if self.filtered_images and self.current_index > 0:
            self.current_index -= 1
            self.pan_x = self.pan_y = 0
            self.zoom_level = 1.0
            self.zoom_val.config(text="100%")
            self.canvas_image_id = None
            self.display_current_image()
            self._focus_tree_on_index(self.current_index)

    def next_img(self):
        if self.filtered_images and self.current_index < len(self.filtered_images) - 1:
            self.current_index += 1
            self.pan_x = self.pan_y = 0
            self.zoom_level = 1.0
            self.zoom_val.config(text="100%")
            self.canvas_image_id = None
            self.display_current_image()
            self._focus_tree_on_index(self.current_index)

def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description='ImageFlow - Fast Image Viewer')
    parser.add_argument('image', nargs='?', type=str, help='Image file to open')
    parser.add_argument('--image', dest='image_flag', type=str, help='Image file to open')
    parser.add_argument('--folder', type=str, help='Folder to open')
    parser.add_argument('--txt', type=str, help='Text file with image names to select')
    args = parser.parse_args()
    if args.image_flag:
        args.image = args.image_flag
    return args

def main():
    args = parse_arguments()
    root = tk.Tk()
    app = ImageGallery(root, cli_args=args)
    root.mainloop()

if __name__ == "__main__":
    main()
