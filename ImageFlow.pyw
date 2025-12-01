import tkinter as tk
from tkinter import filedialog, ttk
from PIL import Image, ImageTk
import os
from collections import OrderedDict
import threading
from natsort import natsorted
import queue
import mmap
import io
from concurrent.futures import ThreadPoolExecutor
import struct

class FastImageCache:
    """Ultra-fast image cache with memory-mapped files"""
    def __init__(self, max_size=200):
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
    def __init__(self, root):
        self.root = root
        self.root.title("Image Gallery Viewer")
        self.root.geometry("1200x800")
        
        try:
            self.root.iconbitmap("gallery_icon.ico")
        except:
            pass
        
        # Core data
        self.images = []
        self.filtered_images = []
        self.current_index = 0
        self.image_folder = ""
        
        # View state
        self.zoom_level = 1.0
        self.sidebar_visible = True
        self.dark_mode = True
        self.fullscreen_mode = False
        self.grid_mode = True
        self.single_view_mode = False
        
        # Pan state
        self.zoom_pan_timer = None
        self.pan_x = 0
        self.pan_y = 0
        self.pan_start_x = 0
        self.pan_start_y = 0
        
        # Ultra-optimized caching
        self.thumb_cache = FastImageCache(max_size=400)
        self.img_cache = FastImageCache(max_size=30)
        self.aspect_cache = {}
        self.photos = []
        
        # Layout settings
        self.base_width = 200
        self.gap = 4
        self.num_columns = 4
        
        # Async operations
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.thumb_queue = queue.Queue(maxsize=100)
        self.result_queue = queue.Queue()
        self.stop_workers = False
        
        # Render optimization
        self.render_id = None
        self.scroll_id = None
        self.image_positions = {}
        self.visible_thumbs = {}
        
        # Ghost arrows
        self.ghost_arrow_left = None
        self.ghost_arrow_right = None
        self.ghost_timer = None
        self.last_size = None
        
        # Colors
        self.colors = {
            'light': {'bg': '#ffffff', 'sidebar': '#f5f5f5', 'header': '#e8e8e8',
                     'text': '#000000', 'button': '#0078d4', 'btn_txt': '#ffffff'},
            'dark': {'bg': '#1e1e1e', 'sidebar': '#252526', 'header': '#2d2d30',
                    'text': '#e0e0e0', 'button': '#0078d4', 'btn_txt': '#ffffff'}
        }
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.build_ui()
        self.apply_theme()
        
        # Start workers
        self.start_workers()
        
        # Bindings
        self.root.bind("<Escape>", self.on_escape)
        self.root.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.root.bind("<Left>", lambda e: self.prev_img() if self.single_view_mode else None)
        self.root.bind("<Right>", lambda e: self.next_img() if self.single_view_mode else None)
        
        # Start result checker
        self.check_results()

    def start_workers(self):
        """Start background thumbnail workers"""
        for _ in range(3):
            t = threading.Thread(target=self.thumbnail_worker, daemon=True)
            t.start()

    def thumbnail_worker(self):
        """Worker thread for generating thumbnails"""
        while not self.stop_workers:
            try:
                task = self.thumb_queue.get(timeout=0.5)
                if task is None:
                    break
                
                path, width, priority = task
                key = f"{path}_{width}"
                
                # Skip if already cached
                if self.thumb_cache.get(key):
                    continue
                
                try:
                    # Fast thumbnail generation
                    with Image.open(path) as img:
                        aspect = img.width / img.height
                        new_h = int(width / aspect)
                        
                        # Use NEAREST for faster processing on lower priority
                        resample = Image.Resampling.LANCZOS if priority > 5 else Image.Resampling.BILINEAR
                        thumb = img.resize((width, new_h), resample)
                        
                        self.result_queue.put((key, thumb, path))
                except Exception as e:
                    pass
                
                self.thumb_queue.task_done()
            except queue.Empty:
                continue

    def check_results(self):
        """Check for completed thumbnails"""
        try:
            count = 0
            while count < 10:  # Process up to 10 per check
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
        
        self.root.after(50, self.check_results)

    def get_aspect_ratio_fast(self, path):
        """Ultra-fast aspect ratio without loading full image"""
        if path in self.aspect_cache:
            return self.aspect_cache[path]
        
        try:
            # Just peek at image header
            with Image.open(path) as img:
                ratio = img.width / img.height
            self.aspect_cache[path] = ratio
            return ratio
        except:
            return 1.0

    def queue_thumbnail(self, path, width, priority=5):
        """Queue thumbnail for generation"""
        try:
            self.thumb_queue.put_nowait((path, width, priority))
        except queue.Full:
            pass

    def get_thumbnail(self, path, width):
        """Get thumbnail from cache or queue for generation"""
        key = f"{path}_{width}"
        thumb = self.thumb_cache.get(key)
        return thumb

    def get_image(self, path):
        """Get full image with caching"""
        cached = self.img_cache.get(path)
        if cached:
            return cached
        
        try:
            img = Image.open(path)
            self.img_cache.put(path, img)
            return img
        except:
            return None

    def preload_adjacent(self):
        """Preload images adjacent to current"""
        if not self.single_view_mode or not self.filtered_images:
            return
        
        for offset in [-2, -1, 1, 2]:
            idx = self.current_index + offset
            if 0 <= idx < len(self.filtered_images):
                path = self.filtered_images[idx]['path']
                threading.Thread(target=self._preload_single, args=(path,), daemon=True).start()

    def _preload_single(self, path):
        """Preload a single image"""
        if not self.img_cache.get(path):
            try:
                img = Image.open(path)
                self.img_cache.put(path, img)
            except:
                pass

    def on_escape(self, event):
        if self.single_view_mode:
            if self.view_mode.get() == "selected" and len(self.filtered_images) == 1:
                self.view_mode.set("all")
                self.update_view()
            else:
                self.back_to_grid()
        elif self.fullscreen_mode:
            self.exit_fullscreen()

    def on_close(self):
        self.stop_workers = True
        
        # Stop workers
        for _ in range(3):
            try:
                self.thumb_queue.put(None, block=False)
            except:
                pass
        
        self.executor.shutdown(wait=False)
        
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
        
        # Toolbar
        self.toolbar = tk.Frame(self.root, height=60)
        self.toolbar.pack(side=tk.TOP, fill=tk.X, before=self.main)
        self.toolbar.pack_propagate(False)
        
        left = tk.Frame(self.toolbar)
        left.pack(side=tk.LEFT, padx=10, pady=10)
        
        tk.Button(left, text="Select Folder", command=self.select_folder, relief=tk.FLAT,
                 padx=15, pady=10, cursor="hand2", font=("Segoe UI", 11)).pack(side=tk.LEFT, padx=5)
        
        self.deselect_btn = tk.Button(left, text="Deselect All", command=self.deselect_all, relief=tk.FLAT,
                 padx=12, pady=10, cursor="hand2", font=("Segoe UI", 11), state=tk.DISABLED)
        self.deselect_btn.pack(side=tk.LEFT, padx=5)
        
        self.back_btn = tk.Button(left, text="Back to Grid", command=self.back_to_grid,
                                  relief=tk.FLAT, padx=12, pady=10, cursor="hand2", font=("Segoe UI", 11))
        self.back_btn.pack(side=tk.LEFT, padx=5)
        
        right = tk.Frame(self.toolbar)
        right.pack(side=tk.RIGHT, padx=10, pady=10)
        
        self.full_btn = tk.Button(right, text="Focus Mode", command=self.toggle_fullscreen,
                                  relief=tk.FLAT, padx=12, pady=10, cursor="hand2", font=("Segoe UI", 11))
        self.full_btn.pack(side=tk.RIGHT, padx=5)
        
        self.dark_btn = tk.Button(right, text="Light Mode" if self.dark_mode else "Dark Mode", 
                                  command=self.toggle_dark, relief=tk.FLAT, padx=12, pady=10, 
                                  cursor="hand2", font=("Segoe UI", 11))
        self.dark_btn.pack(side=tk.RIGHT, padx=5)
        
        self.side_btn = tk.Button(right, text="Hide Panel", command=self.toggle_sidebar,
                                  relief=tk.FLAT, padx=12, pady=10, cursor="hand2", font=("Segoe UI", 11))
        self.side_btn.pack(side=tk.RIGHT, padx=5)
        
        # Zoom controls
        self.zoom_txt = tk.Label(right, text="Zoom:", font=("Segoe UI", 11))
        self.zoom_out = tk.Button(right, text="-", command=self.zoom_out_fn, width=3,
                                  relief=tk.FLAT, cursor="hand2", pady=8, font=("Segoe UI", 11))
        self.zoom_val = tk.Label(right, text="100%", width=5, font=("Segoe UI", 11))
        self.zoom_in = tk.Button(right, text="+", command=self.zoom_in_fn, width=3,
                                relief=tk.FLAT, cursor="hand2", pady=8, font=("Segoe UI", 11))
        
        # Sidebar
        self.sidebar = tk.Frame(self.main, width=280)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y, in_=self.main)
        self.sidebar.pack_propagate(False)
        
        # Search
        sf = tk.Frame(self.sidebar)
        sf.pack(fill=tk.X, padx=10, pady=10)
        tk.Label(sf, text="Search Images", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, pady=(0,5))
        self.search_var = tk.StringVar()
        self.search_var.trace('w', lambda *a: self.update_view())
        tk.Entry(sf, textvariable=self.search_var, font=("Segoe UI", 11)).pack(fill=tk.X)
        
        # View mode
        mf = tk.Frame(self.sidebar)
        mf.pack(fill=tk.X, padx=10, pady=10)
        tk.Label(mf, text="View Mode", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, pady=5)
        
        self.view_mode = tk.StringVar(value="all")
        self.view_mode.trace('w', self.on_view_mode_change)
        tk.Radiobutton(mf, text="All Images", variable=self.view_mode, value="all",
                      command=self.update_view, cursor="hand2", font=("Segoe UI", 11)).pack(anchor=tk.W, pady=2)
        tk.Radiobutton(mf, text="Selected Only", variable=self.view_mode, value="selected",
                      command=self.update_view, cursor="hand2", font=("Segoe UI", 11)).pack(anchor=tk.W, pady=2)
        
        # Columns
        cf = tk.Frame(self.sidebar)
        cf.pack(fill=tk.X, padx=10, pady=10)
        tk.Label(cf, text="Columns", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, pady=5)
        
        col_frame = tk.Frame(cf)
        col_frame.pack(fill=tk.X)
        
        self.col_var = tk.IntVar(value=4)
        self.col_radios = {}
        for i in [3, 4, 5, 6]:
            rb = tk.Radiobutton(col_frame, text=str(i), variable=self.col_var, value=i,
                          command=self.on_column_change, cursor="hand2", font=("Segoe UI", 10))
            rb.pack(side=tk.LEFT, padx=5)
            self.col_radios[i] = rb
        
        # Image list
        tk.Label(self.sidebar, text="Images", font=("Segoe UI", 11, "bold")).pack(padx=10, pady=5, anchor=tk.W)
        
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
        self.scrollbar.config(command=self.on_scroll)
        
        self.canvas.bind("<ButtonPress-1>", self.on_click)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Button-4>", self.on_wheel)
        self.canvas.bind("<Button-5>", self.on_wheel)
        self.canvas.bind("<Configure>", lambda e: self.schedule_render())
        self.canvas.bind("<Double-Button-1>", lambda e: self.toggle_fullscreen())
        
        # Navigation
        self.nav_frame = tk.Frame(self.display, height=60)
        self.nav_frame.pack(fill=tk.X, padx=10, pady=10)
        self.nav_frame.pack_propagate(False)
        
        self.prev_btn = tk.Button(self.nav_frame, text="Previous", command=self.prev_img, padx=20, pady=8,
                                  relief=tk.FLAT, cursor="hand2", font=("Segoe UI", 11))
        self.prev_btn.pack(side=tk.LEFT, padx=5)
        
        self.info = tk.Label(self.nav_frame, text="Select a folder to begin", font=("Segoe UI", 11))
        self.info.pack(side=tk.LEFT, expand=True)
        
        self.next_btn = tk.Button(self.nav_frame, text="Next", command=self.next_img, padx=20, pady=8,
                                  relief=tk.FLAT, cursor="hand2", font=("Segoe UI", 11))
        self.next_btn.pack(side=tk.RIGHT, padx=5)
        
        self.update_ui_state()

    def update_ui_state(self):
        """Update UI element visibility"""
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
        else:
            self.back_btn.pack_forget()
            self.zoom_txt.pack_forget()
            self.zoom_out.pack_forget()
            self.zoom_val.pack_forget()
            self.zoom_in.pack_forget()
            self.prev_btn.pack_forget()
            self.next_btn.pack_forget()
        
        if self.view_mode.get() == "selected":
            any_selected = any(img['selected'] for img in self.images)
            self.deselect_btn.config(state=tk.NORMAL if any_selected else tk.DISABLED)
        else:
            self.deselect_btn.config(state=tk.DISABLED)

    def on_view_mode_change(self, *args):
        """Handle view mode changes"""
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

    def show_ghost_arrows(self):
        """Show navigation arrows in fullscreen"""
        if not self.fullscreen_mode or not self.single_view_mode or not self.filtered_images:
            return
        
        if self.ghost_timer:
            self.root.after_cancel(self.ghost_timer)
            self.ghost_timer = None
        
        self.hide_ghost_arrows()
        
        arrow_bg = self.get_color('bg')
        arrow_fg = self.get_color('text')
        
        if self.current_index > 0:
            self.ghost_arrow_left = tk.Button(
                self.canvas, text="<", command=self.prev_img,
                font=("Segoe UI", 20, "bold"), bg=arrow_bg, fg=arrow_fg,
                relief=tk.FLAT, bd=0, highlightthickness=1,
                highlightbackground=arrow_fg, cursor="hand2", width=3, height=2
            )
            self.ghost_arrow_left.place(relx=0.02, rely=0.5, anchor="w")
        
        if self.current_index < len(self.filtered_images) - 1:
            self.ghost_arrow_right = tk.Button(
                self.canvas, text=">", command=self.next_img,
                font=("Segoe UI", 20, "bold"), bg=arrow_bg, fg=arrow_fg,
                relief=tk.FLAT, bd=0, highlightthickness=1,
                highlightbackground=arrow_fg, cursor="hand2", width=3, height=2
            )
            self.ghost_arrow_right.place(relx=0.98, rely=0.5, anchor="e")
        
        self.ghost_timer = self.root.after(3000, self.hide_ghost_arrows)

    def hide_ghost_arrows(self):
        """Hide navigation arrows"""
        if self.ghost_arrow_left:
            self.ghost_arrow_left.destroy()
            self.ghost_arrow_left = None
        if self.ghost_arrow_right:
            self.ghost_arrow_right.destroy()
            self.ghost_arrow_right = None
        if self.ghost_timer:
            self.root.after_cancel(self.ghost_timer)
            self.ghost_timer = None

    def on_scroll(self, *args):
        self.canvas.yview(*args)
        self.schedule_render()

    def schedule_render(self):
        """Debounced render scheduling"""
        if self.scroll_id:
            self.root.after_cancel(self.scroll_id)
        self.scroll_id = self.root.after(30, self.render)

    def on_wheel(self, event):
        """Mouse wheel handler"""
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
            self.schedule_render()

    def on_click(self, event):
        """Canvas click handler"""
        if self.grid_mode:
            self.handle_grid_click(event)
        elif self.single_view_mode:
            if self.zoom_level > 1.0:
                self.pan_start_x = event.x
                self.pan_start_y = event.y
            elif self.fullscreen_mode:
                self.show_ghost_arrows()

    def on_drag(self, event):
        """Drag handler for panning"""
        if self.single_view_mode and self.zoom_level > 1.0:
            self.pan_x += event.x - self.pan_start_x
            self.pan_y += event.y - self.pan_start_y
            self.pan_start_x = event.x
            self.pan_start_y = event.y
            self.debounced_display()
            
    def debounced_display(self):
        """Debounce display updates to prevent flickering"""
        if self.zoom_pan_timer:
            self.root.after_cancel(self.zoom_pan_timer)
        
        # Only move existing image, never clear during pan/zoom
        self.instant_transform()
        
        # Schedule full quality render only after 150ms of no movement
        self.zoom_pan_timer = self.root.after(150, self.render_high_quality)
        
    def instant_transform(self):
        """Instant pan/zoom transform - ZERO canvas operations, just move existing image"""
        if not self.single_view_mode:
            return
        
        try:
            cw = max(self.canvas.winfo_width(), 100)
            ch = max(self.canvas.winfo_height(), 100)
            
            x = cw // 2 + self.pan_x
            y = ch // 2 + self.pan_y
            
            # Just move the existing image - NO delete, NO create
            items = self.canvas.find_all()
            if items:
                self.canvas.coords(items[0], x, y)
        except:
            pass

    def render_high_quality(self):
        """Render high quality image only after movement stops - prevents unnecessary work"""
        if not self.filtered_images or self.current_index >= len(self.filtered_images):
            return
        
        img_data = self.filtered_images[self.current_index]
        path = img_data['path']
        
        # Get image
        img = self.get_image(path)
        if not img:
            return
        
        self.root.update_idletasks()
        
        cw = max(self.canvas.winfo_width(), 100)
        ch = max(self.canvas.winfo_height(), 100)
        iw, ih = img.size
        
        # Calculate scaled size
        scale = min(cw / iw, ch / ih) * self.zoom_level
        new_w = max(1, int(iw * scale))
        new_h = max(1, int(ih * scale))
        
        # Check if we actually need to resize (avoid unnecessary work)
        if self.photos and hasattr(self, 'last_size'):
            if self.last_size == (new_w, new_h, path):
                # Same size and image, just reposition
                self.instant_transform()
                return
        
        # Store last size
        self.last_size = (new_w, new_h, path)
        
        # Resize
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        new_photo = ImageTk.PhotoImage(resized)
        
        # Atomic swap: create new image at same position as old, then delete old
        x = cw // 2 + self.pan_x
        y = ch // 2 + self.pan_y
        
        # Create new first, delete old second - single frame update
        old_items = self.canvas.find_all()
        new_item = self.canvas.create_image(x, y, anchor=tk.CENTER, image=new_photo)
        for item in old_items:
            self.canvas.delete(item)
        
        # Update photo reference
        self.photos.clear()
        self.photos.append(new_photo)
        
        # Update info
        self.info.config(text=f"{self.current_index + 1} / {len(self.filtered_images)} - {img_data['name']}")
        
        # Preload adjacent
        self.preload_adjacent()

    # --- FIX APPLIED HERE ---
    def handle_grid_click(self, event):
        """Handle click on grid image, converting window to canvas coordinates."""
        
        # Convert window coordinates (event.x, event.y) to canvas coordinates
        click_x = self.canvas.canvasx(event.x)
        click_y = self.canvas.canvasy(event.y)
        
        # Find clicked image
        for path, (x, y, w, h) in self.image_positions.items():
            if x <= click_x <= x + w and y <= click_y <= y + h:
                # Find the corresponding image data
                img_data = next((img for img in self.filtered_images if img['path'] == path), None)
                if img_data:
                    self.show_single_img(img_data)
                    return
    # --------------------------

    def show_single_img(self, img_data):
        """Display single image"""
        if img_data in self.filtered_images:
            self.current_index = self.filtered_images.index(img_data)
            self.single_view_mode = True
            self.grid_mode = False
            self.zoom_level = 1.0
            self.zoom_val.config(text="100%")
            self.pan_x = self.pan_y = 0
            self.update_ui_state()
            self.display_current_image()

    def back_to_grid(self):
        """Return to grid view"""
        self.single_view_mode = False
        self.grid_mode = True
        self.zoom_level = 1.0
        self.pan_x = self.pan_y = 0
        self.hide_ghost_arrows()
        self.update_ui_state()
        self.render()

    def zoom_in_fn(self):
        self.zoom_level = min(5.0, self.zoom_level + 0.25)
        self.zoom_val.config(text=f"{int(self.zoom_level * 100)}%")
        self.debounced_display()

    def zoom_out_fn(self):
        self.zoom_level = max(0.25, self.zoom_level - 0.25)
        self.zoom_val.config(text=f"{int(self.zoom_level * 100)}%")
        if self.zoom_level <= 1.0:
            self.pan_x = 0
            self.pan_y = 0
        self.debounced_display()
    
    def apply_theme(self):
        """Apply color theme"""
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
        
        for child in self.toolbar.winfo_children():
            try:
                child.configure(bg=hd)
            except:
                pass
            for btn_child in child.winfo_children():
                try:
                    if isinstance(btn_child, tk.Button):
                        btn_child.configure(bg=btn, fg=btn_txt, activebackground=btn, activeforeground=btn_txt)
                    elif isinstance(btn_child, tk.Label):
                        btn_child.configure(bg=hd, fg=tx)
                except:
                    pass
        
        self._apply_sidebar_theme(sb, tx)
        
        self.prev_btn.configure(bg=btn, fg=btn_txt, activebackground=btn, activeforeground=btn_txt)
        self.next_btn.configure(bg=btn, fg=btn_txt, activebackground=btn, activeforeground=btn_txt)
        
        # Treeview theme
        style = ttk.Style()
        if self.dark_mode:
            style.theme_use('default')
            style.configure("Treeview", background="#2d2d30", foreground="#e0e0e0",
                          fieldbackground="#2d2d30", rowheight=25, borderwidth=0)
            style.map('Treeview', background=[('selected', '#0078d4')])
            style.configure("Treeview.Heading", background="#3d3d40", foreground="#e0e0e0", relief="flat")
            style.map("Treeview.Heading", background=[('active', '#4d4d50')])
        else:
            style.theme_use('default')
            style.configure("Treeview", background="#ffffff", foreground="#000000",
                          fieldbackground="#ffffff", rowheight=25, borderwidth=0)
            style.map('Treeview', background=[('selected', '#0078d4')])
            style.configure("Treeview.Heading", background="#e8e8e8", foreground="#000000", relief="flat")
            style.map("Treeview.Heading", background=[('active', '#d8d8d8')])

    def _apply_sidebar_theme(self, bg, fg):
        """Apply theme to sidebar recursively"""
        for child in self.sidebar.winfo_children():
            self._apply_theme_recursive(child, bg, fg)

    def _apply_theme_recursive(self, widget, bg, fg):
        """Recursively apply theme to widget and children"""
        try:
            if isinstance(widget, (tk.Frame, tk.Label)):
                if isinstance(widget, tk.Label):
                    widget.configure(bg=bg, fg=fg)
                else:
                    widget.configure(bg=bg)
            elif isinstance(widget, tk.Entry):
                entry_bg = '#3d3d40' if self.dark_mode else '#ffffff'
                entry_fg = '#e0e0e0' if self.dark_mode else '#000000'
                widget.configure(bg=entry_bg, fg=entry_fg, 
                               insertbackground=entry_fg,
                               selectbackground='#0078d4',
                               selectforeground='#ffffff')
            elif isinstance(widget, tk.Radiobutton):
                widget.configure(bg=bg, fg=fg, selectcolor=bg, 
                               activebackground=bg, activeforeground=fg)
            elif isinstance(widget, tk.Button):
                btn_bg = self.get_color('button')
                btn_fg = self.get_color('btn_txt')
                widget.configure(bg=btn_bg, fg=btn_fg, 
                               activebackground=btn_bg, activeforeground=btn_fg)
            elif isinstance(widget, tk.Scrollbar):
                widget.configure(bg=bg, troughcolor=bg,
                               activebackground=bg if self.dark_mode else '#e0e0e0')
        except:
            pass
        
        for child in widget.winfo_children():
            self._apply_theme_recursive(child, bg, fg)

    def toggle_dark(self):
        self.dark_mode = not self.dark_mode
        self.dark_btn.config(text="Light Mode" if self.dark_mode else "Dark Mode")
        self.apply_theme()
        self.render()

    def toggle_sidebar(self):
        if self.sidebar_visible:
            self.sidebar.pack_forget()
            self.side_btn.config(text="Show Panel")
        else:
            self.sidebar.pack(side=tk.LEFT, fill=tk.Y, in_=self.main, before=self.display)
            self.side_btn.config(text="Hide Panel")
        self.sidebar_visible = not self.sidebar_visible
        self.render()

    def toggle_fullscreen(self):
        if self.fullscreen_mode:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self):
        self.fullscreen_mode = True
        self.root.attributes('-fullscreen', True)
        self.toolbar.pack_forget()
        if self.sidebar_visible:
            self.sidebar.pack_forget()
        self.nav_frame.pack_forget()
        self.full_btn.config(text="Exit Focus")
        self.render()
        if self.single_view_mode:
            self.show_ghost_arrows()

    def exit_fullscreen(self):
        self.fullscreen_mode = False
        self.root.attributes('-fullscreen', False)
        self.toolbar.pack(side=tk.TOP, fill=tk.X, before=self.main)
        if self.sidebar_visible:
            self.sidebar.pack(side=tk.LEFT, fill=tk.Y, in_=self.main, before=self.display)
        self.nav_frame.pack(fill=tk.X, padx=10, pady=10)
        self.full_btn.config(text="Focus Mode")
        self.hide_ghost_arrows()
        self.render()

    def select_folder(self):
        folder = filedialog.askdirectory(title="Select Image Folder")
        if folder:
            self.image_folder = folder
            self.load_images()

    def load_images(self):
        """Ultra-fast image loading - instant folder scan"""
        self.images.clear()
        self.filtered_images.clear()
        self.aspect_cache.clear()
        self.image_positions.clear()
        
        # Clear tree
        self.tree.delete(*self.tree.get_children())
        
        exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tiff'}
        
        self.info.config(text="Scanning folder...")
        self.root.update_idletasks()
        
        # Ultra-fast OS directory scan
        files = []
        try:
            with os.scandir(self.image_folder) as entries:
                for entry in entries:
                    if entry.is_file():
                        ext = os.path.splitext(entry.name)[1].lower()
                        if ext in exts:
                            files.append({
                                'name': entry.name, 
                                'path': entry.path, 
                                'selected': False
                            })
        except Exception as e:
            self.info.config(text=f"Error: {e}")
            return
        
        # Natural sort
        files = natsorted(files, key=lambda x: x['name'])
        self.images = files
        
        # Instant tree population (show loading indicator)
        self.info.config(text=f"Loaded {len(self.images)} images")
        
        # Populate tree asynchronously
        self.populate_tree_async()
        
        self.update_view()

    def populate_tree_async(self):
        """Populate tree view asynchronously for instant feel"""
        batch = 200
        
        def add_items(start):
            end = min(start + batch, len(self.images))
            items = []
            
            for i in range(start, end):
                img = self.images[i]
                sel = "✓" if img['selected'] else ""
                items.append((str(i + 1), sel, img['name']))
            
            # Batch insert
            for vals in items:
                self.tree.insert('', 'end', values=vals)
            
            if end < len(self.images):
                self.root.after(1, lambda: add_items(end))
        
        if self.images:
            add_items(0)

    def update_view(self):
        """Update filtered view"""
        search = self.search_var.get().lower()
        mode = self.view_mode.get()
        
        self.filtered_images = []
        
        for img in self.images:
            matches_search = not search or search in img['name'].lower()
            matches_mode = mode != "selected" or img['selected']
            
            if matches_search and matches_mode:
                self.filtered_images.append(img)
        
        # Update tree
        self.tree.delete(*self.tree.get_children())
        
        for idx, img in enumerate(self.filtered_images, 1):
            sel = "✓" if img['selected'] else ""
            self.tree.insert('', 'end', values=(str(idx), sel, img['name']))
        
        # Handle selection mode
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

    def on_tree_click(self, event):
        """Tree click handler for selection"""
        region = self.tree.identify_region(event.x, event.y)
        item = self.tree.identify_row(event.y)
        
        if item and region == "cell":
            column = self.tree.identify_column(event.x)
            values = self.tree.item(item, 'values')
            
            if column == '#2':  # Selection column
                idx = int(values[0]) - 1
                if 0 <= idx < len(self.filtered_images):
                    img_data = self.filtered_images[idx]
                    img_data['selected'] = not img_data['selected']
                    
                    # Update main list
                    for img in self.images:
                        if img['path'] == img_data['path']:
                            img['selected'] = img_data['selected']
                            break
                    
                    # Update display
                    sel = "✓" if img_data['selected'] else ""
                    self.tree.item(item, values=(values[0], sel, values[2]))
                    
                    self.update_view()

    # --- FIX APPLIED HERE ---
    def on_tree_dbl(self, event):
        """Tree double-click handler"""
        item = self.tree.identify_row(event.y)
        if item:
            values = self.tree.item(item, 'values')
            if values:
                # The index in filtered_images is values[0] - 1
                try:
                    idx = int(values[0]) - 1
                    if 0 <= idx < len(self.filtered_images):
                        self.show_single_img(self.filtered_images[idx])
                except ValueError:
                    # Should not happen if populate_tree_async works correctly
                    pass
    # --------------------------

    def deselect_all(self):
        """Deselect all images"""
        for img in self.images:
            img['selected'] = False
        
        for item in self.tree.get_children():
            values = self.tree.item(item, 'values')
            self.tree.item(item, values=(values[0], "", values[2]))
        
        self.view_mode.set("all")
        self.update_view()

    def render(self):
        """Main render dispatcher"""
        if self.single_view_mode:
            self.show_single()
            return
        
        self.canvas.delete("all")
        self.photos.clear()
        self.image_positions.clear()
        
        if not self.filtered_images:
            self.canvas.create_text(
                self.canvas.winfo_width() // 2, 
                self.canvas.winfo_height() // 2,
                text="No images to display\nSelect a folder to begin",
                font=("Segoe UI", 14),
                fill=self.get_color('text'),
                justify=tk.CENTER
            )
            self.canvas.config(scrollregion=(0, 0, self.canvas.winfo_width(), self.canvas.winfo_height()))
            return
        
        self.calculate_layout()
        self.update_visible_thumbs()

    def calculate_layout(self):
        """Calculate masonry layout positions (fast, no image loading)"""
        canvas_width = self.canvas.winfo_width()
        if canvas_width <= 1:
            self.root.after(50, self.calculate_layout)
            return
        
        # Determine columns
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
        # Ensure column width calculation is safe
        if cols == 0:
            cols = 1 # Fallback to a single column if somehow 0
        col_w = (canvas_width - (gap * (cols + 1))) // cols
        col_w = max(50, col_w)
        
        # Calculate positions
        col_heights = [gap] * cols
        
        self.image_positions.clear() # Clear positions before recalculating
        
        for img in self.filtered_images:
            path = img['path']
            
            # Find shortest column
            min_col = col_heights.index(min(col_heights))
            
            # Get aspect ratio
            aspect = self.get_aspect_ratio_fast(path)
            img_h = int(col_w / aspect)
            
            # Position
            x = gap + min_col * (col_w + gap)
            y = col_heights[min_col]
            
            self.image_positions[path] = (x, y, col_w, img_h)
            col_heights[min_col] = y + img_h + gap
        
        # Set scroll region
        total_h = max(col_heights) + gap if col_heights else 0
        self.canvas.config(scrollregion=(0, 0, canvas_width, total_h))

    # --- FIX APPLIED HERE ---
    def update_visible_thumbs(self):
        """Render only visible thumbnails"""
        canvas_h = self.canvas.winfo_height()
        
        # Get canvas scroll region total height
        sr = self.canvas.cget("scrollregion")
        if not sr: # Check if scrollregion is set
            total_h = canvas_h 
        else:
            sr_parts = sr.split()
            total_h = int(float(sr_parts[3])) if len(sr_parts) >= 4 else canvas_h

        # Get vertical scroll fraction
        st_fraction = self.canvas.yview()[0]
        
        # Calculate visible area in canvas coordinates
        visible_top = st_fraction * total_h
        visible_bottom = visible_top + canvas_h + 600 # Add buffer (300 above, 300 below)
        visible_top -= 300
        
        # Clear canvas
        self.canvas.delete("all")
        self.photos.clear()
        
        # Render visible items
        for path, (x, y, w, h) in self.image_positions.items():
            if y + h < visible_top or y > visible_bottom:
                continue
            
            # Try to get cached thumbnail
            thumb = self.get_thumbnail(path, w)
            
            if thumb:
                try:
                    photo = ImageTk.PhotoImage(thumb)
                    self.photos.append(photo)
                    
                    # Highlight if selected
                    img_data = next((img for img in self.filtered_images if img['path'] == path), None)
                    if img_data and img_data['selected']:
                        self.canvas.create_rectangle(x-2, y-2, x+w+2, y+h+2, 
                                                   outline="#0078d4", width=3)
                    
                    self.canvas.create_image(x, y, anchor=tk.NW, image=photo)
                except:
                    pass
            else:
                # Placeholder
                self.canvas.create_rectangle(x, y, x+w, y+h, 
                                           fill=self.get_color('sidebar'), 
                                           outline=self.get_color('text'))
                # Queue for loading
                # Priority: 10 for currently visible area, 5 for buffer
                priority = 10 if (visible_top + 300) <= y <= (visible_bottom - 300) else 5
                self.queue_thumbnail(path, w, priority)
    # --------------------------

    def show_single(self):
        """Show single image view"""
        if not self.filtered_images:
            return
    
        # Reset scrollbar for single view
        self.canvas.yview_moveto(0)
    
        self.display_current_image()

    def display_current_image(self):
        """Initial display of current image when switching images"""
        if not self.filtered_images or self.current_index >= len(self.filtered_images):
            return
        
        img_data = self.filtered_images[self.current_index]
        path = img_data['path']
        
        # Reset scroll region
        self.canvas.config(scrollregion=(0, 0, 0, 0))
        
        # Clear canvas for new image
        self.canvas.delete("all")
        self.photos.clear()
        
        # Get image
        img = self.get_image(path)
        if not img:
            return
        
        self.root.update_idletasks()
        
        cw = max(self.canvas.winfo_width(), 100)
        ch = max(self.canvas.winfo_height(), 100)
        iw, ih = img.size
        
        # Calculate scaled size
        scale = min(cw / iw, ch / ih) * self.zoom_level
        new_w = max(1, int(iw * scale))
        new_h = max(1, int(ih * scale))
        
        # Store size for future comparison
        self.last_size = (new_w, new_h, path)
        
        # Resize
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(resized)
        self.photos.append(photo)
        
        # Center
        x = cw // 2 + self.pan_x
        y = ch // 2 + self.pan_y
        
        self.canvas.create_image(x, y, anchor=tk.CENTER, image=photo)
        
        # Update info
        self.info.config(text=f"{self.current_index + 1} / {len(self.filtered_images)} - {img_data['name']}")
        
        # Preload adjacent
        self.preload_adjacent()

    def prev_img(self):
        """Previous image"""
        if self.filtered_images and self.current_index > 0:
            self.current_index -= 1
            self.pan_x = self.pan_y = 0
            self.zoom_level = 1.0 # Reset zoom on image change
            self.zoom_val.config(text="100%")
            self.display_current_image()
            if self.fullscreen_mode:
                self.show_ghost_arrows()

    def next_img(self):
        """Next image"""
        if self.filtered_images and self.current_index < len(self.filtered_images) - 1:
            self.current_index += 1
            self.pan_x = self.pan_y = 0
            self.zoom_level = 1.0 # Reset zoom on image change
            self.zoom_val.config(text="100%")
            self.display_current_image()
            if self.fullscreen_mode:
                self.show_ghost_arrows()

def main():
    root = tk.Tk()
    app = ImageGallery(root)
    root.mainloop()

if __name__ == "__main__":
    main()