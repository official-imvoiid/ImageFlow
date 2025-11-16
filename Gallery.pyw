import tkinter as tk
from tkinter import filedialog, ttk
from PIL import Image, ImageTk
import os
from datetime import datetime
import sys
import threading
import re

class ImageGallery:
    def __init__(self, root):
        self.root = root
        self.root.title("Image Gallery")
        self.root.geometry("1200x800")
       
        # Set window icon and make it proper application
        try:
            self.root.iconbitmap("gallery_icon.ico") # You can add an icon file
        except:
            pass
       
        self.images = []
        self.current_index = 0
        self.filtered_images = []
        self.image_folder = ""
        self.zoom_level = 1.0
        self.sidebar_visible = True
        self.sidebar_was_visible = True
        self.dark_mode = False
        self.fullscreen_mode = False
        self.photo_references = []
        self.checkbutton_vars = []
        self.grid_mode = False
        self.pan_start_x = 0
        self.pan_start_y = 0
        self.image_offset_x = 0
        self.image_offset_y = 0
        self.left_arrow = None
        self.right_arrow = None
        self.image_cache = {}
        self.loading_thread = None
        self.stop_loading = False
        self.last_resize_time = 0
        self.resize_after_id = None
        self.arrow_timeout_id = None
        self.sidebar_toggle_after_id = None
        self.sidebar_animation_id = None
        self.sidebar_target_width = 280
        self.current_sidebar_width = 280
       
        # Color schemes
        self.colors = {
            'light': {
                'bg': '#ffffff',
                'sidebar': '#f5f5f5',
                'header': '#e8e8e8',
                'text': '#000000',
                'text_secondary': '#666666',
                'button': '#0078d4',
                'button_text': '#ffffff',
                'border': '#d1d1d1',
                'entry_bg': '#ffffff',
                'tree_bg': '#ffffff',
                'tree_fg': '#000000',
                'arrow_bg': '#ffffff',
                'arrow_fg': '#000000'
            },
            'dark': {
                'bg': '#1e1e1e',
                'sidebar': '#252526',
                'header': '#2d2d2d',
                'text': '#e0e0e0',
                'text_secondary': '#a0a0a0',
                'button': '#0078d4',
                'button_text': '#ffffff',
                'border': '#3d3d3d',
                'entry_bg': '#3c3c3c',
                'tree_bg': '#252526',
                'tree_fg': '#e0e0e0',
                'arrow_bg': '#1e1e1e',
                'arrow_fg': '#e0e0e0'
            }
        }
       
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.create_widgets()
        self.apply_theme()
       
        # Bind Escape and F11 keys
        self.root.bind("<Escape>", lambda e: self.exit_fullscreen())
        self.root.bind("<F11>", lambda e: self.toggle_fullscreen())
       
        # Bind arrow keys for navigation
        self.root.bind("<Left>", lambda e: self.prev_image() if not self.grid_mode else None)
        self.root.bind("<Right>", lambda e: self.next_image() if not self.grid_mode else None)
       
        # Bind mouse wheel for zoom
        self.image_canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.image_canvas.bind("<Button-4>", self.on_mouse_wheel)
        self.image_canvas.bind("<Button-5>", self.on_mouse_wheel)
       
        # Bind double click for toggle fullscreen
        self.image_canvas.bind("<Double-Button-1>", lambda e: self.toggle_fullscreen())
       
        # Bind window resize for immediate response
        self.root.bind("<Configure>", self.on_window_resize)

    def back_to_grid(self):
        """Return to grid view of selected images"""
        if self.view_mode.get() == "selected":
            self.grid_mode = True
            self.zoom_level = 1.0
            self.zoom_label.config(text="100%")
            self.image_offset_x = 0
            self.image_offset_y = 0
            self.update_nav_visibility()
            self.display_images()
       
    def create_button(self, parent, text, command, icon_size=12, padx=12, pady=8):
        """Create a styled button with consistent appearance"""
        return tk.Button(
            parent,
            text=text,
            command=command,
            padx=padx,
            pady=pady,
            relief=tk.FLAT,
            cursor="hand2",
            font=("Segoe UI", 12)
        )
       
    def on_window_resize(self, event):
        """Handle window resize with immediate response"""
        if event.widget == self.root:
            # Cancel previous resize event
            if self.resize_after_id:
                self.root.after_cancel(self.resize_after_id)
           
            # Immediate display update for better responsiveness
            self.resize_after_id = self.root.after(10, self.display_images)
       
    def on_closing(self):
        """Proper application shutdown"""
        self.stop_loading = True
       
        # Cancel all pending after calls
        if self.resize_after_id:
            self.root.after_cancel(self.resize_after_id)
        if self.arrow_timeout_id:
            self.root.after_cancel(self.arrow_timeout_id)
        if self.sidebar_toggle_after_id:
            self.root.after_cancel(self.sidebar_toggle_after_id)
        if self.sidebar_animation_id:
            self.root.after_cancel(self.sidebar_animation_id)
       
        # Stop loading thread
        if self.loading_thread and self.loading_thread.is_alive():
            self.loading_thread.join(timeout=1)
       
        # Clear image cache to free memory
        self.image_cache.clear()
        self.photo_references.clear()
       
        # Properly destroy the application
        self.root.quit()
        self.root.destroy()
       
        # Force exit to ensure complete cleanup
        os._exit(0)
       
    def get_color(self, key):
        theme = 'dark' if self.dark_mode else 'light'
        return self.colors[theme][key]
       
    def create_widgets(self):
        # Top toolbar
        self.toolbar = tk.Frame(self.root, height=60)
        self.toolbar.pack(fill=tk.X)
        self.toolbar.pack_propagate(False)
       
        # Left section
        left_tools = tk.Frame(self.toolbar)
        left_tools.pack(side=tk.LEFT, padx=10, pady=10)
       
        self.select_folder_btn = self.create_button(left_tools, "📂 Select Folder",
                                                     self.select_folder, icon_size=12, padx=15, pady=10)
        self.select_folder_btn.pack(side=tk.LEFT, padx=5)
       
        self.deselect_all_btn = self.create_button(left_tools, "🚫 Deselect All",
                                                    self.deselect_all, icon_size=12, padx=12, pady=10)
        self.deselect_all_btn.pack(side=tk.LEFT, padx=5)
       
        self.back_to_grid_btn = self.create_button(left_tools, "🔳 Back to Grid",
                                                   self.back_to_grid, icon_size=12, padx=12, pady=10)
       
        # Right section
        right_tools = tk.Frame(self.toolbar)
        right_tools.pack(side=tk.RIGHT, padx=10, pady=10)
       
        self.fullscreen_btn = self.create_button(right_tools, "🔎 Focus Mode",
                                                 self.toggle_fullscreen, icon_size=12, padx=12, pady=10)
        self.fullscreen_btn.pack(side=tk.RIGHT, padx=5)
       
        self.dark_mode_btn = self.create_button(right_tools, "🌜 Dark Mode",
                                               self.toggle_dark_mode, icon_size=12, padx=12, pady=10)
        self.dark_mode_btn.pack(side=tk.RIGHT, padx=5)
       
        self.sidebar_btn = self.create_button(right_tools, "⬅️ Hide Panel",
                                             self.toggle_sidebar, icon_size=12, padx=12, pady=10)
        self.sidebar_btn.pack(side=tk.RIGHT, padx=5)
       
        # Zoom controls
        self.zoom_label_text = tk.Label(right_tools, text="Zoom:", font=("Segoe UI", 12))
        self.zoom_label_text.pack(side=tk.RIGHT, padx=5)
        self.zoom_out_btn = tk.Button(right_tools, text="➖", command=self.zoom_out, width=3,
                 relief=tk.FLAT, cursor="hand2", pady=8, font=("Segoe UI", 12))
        self.zoom_out_btn.pack(side=tk.RIGHT, padx=2)
        self.zoom_label = tk.Label(right_tools, text="100%", width=5, font=("Segoe UI", 12))
        self.zoom_label.pack(side=tk.RIGHT, padx=2)
        self.zoom_in_btn = tk.Button(right_tools, text="➕", command=self.zoom_in, width=3,
                 relief=tk.FLAT, cursor="hand2", pady=8, font=("Segoe UI", 12))
        self.zoom_in_btn.pack(side=tk.RIGHT, padx=2)
       
        # Main container
        self.main_container = tk.Frame(self.root)
        self.main_container.pack(fill=tk.BOTH, expand=True)
       
        # Sidebar
        self.sidebar = tk.Frame(self.main_container, width=280)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar.pack_propagate(False)
       
        # Search box
        search_frame = tk.Frame(self.sidebar)
        search_frame.pack(fill=tk.X, padx=10, pady=10)
       
        tk.Label(search_frame, text="Search Images", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, pady=(0,5))
       
        self.search_var = tk.StringVar()
        self.search_var.trace('w', self.on_search_change)
        self.search_entry = tk.Entry(search_frame, textvariable=self.search_var, font=("Segoe UI", 11))
        self.search_entry.pack(fill=tk.X)
       
        # View mode
        mode_frame = tk.Frame(self.sidebar)
        mode_frame.pack(fill=tk.X, padx=10, pady=10)
       
        tk.Label(mode_frame, text="View Mode", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, pady=5)
       
        self.view_mode = tk.StringVar(value="all")
        modes = [("All Images", "all"), ("Selected Only", "selected")]
        self.radio_buttons = []
        for text, value in modes:
            rb = tk.Radiobutton(mode_frame, text=text, variable=self.view_mode,
                               value=value, command=self.update_view, cursor="hand2", font=("Segoe UI", 11))
            rb.pack(anchor=tk.W, pady=2)
            self.radio_buttons.append(rb)
       
        # Image list with Treeview for checkboxes
        self.images_label = tk.Label(self.sidebar, text="Images", font=("Segoe UI", 11, "bold"))
        self.images_label.pack(padx=10, pady=5, anchor=tk.W)
       
        list_container = tk.Frame(self.sidebar)
        list_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
       
        # Create Treeview for image list with checkboxes
        self.tree_frame = tk.Frame(list_container)
        self.tree_frame.pack(fill=tk.BOTH, expand=True)
       
        # Create scrollbar for treeview
        self.tree_scrollbar = tk.Scrollbar(self.tree_frame)
        self.tree_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
       
        # Create Treeview with two columns: selection and filename
        self.image_tree = ttk.Treeview(
            self.tree_frame,
            columns=('selected', 'filename'),
            show='tree headings',
            yscrollcommand=self.tree_scrollbar.set,
            height=15
        )
        self.tree_scrollbar.config(command=self.image_tree.yview)
       
        # Configure columns
        self.image_tree.column('#0', width=0, stretch=tk.NO) # Hide first empty column
        self.image_tree.column('selected', width=0, stretch=tk.NO) # Hide checkbox column in all mode
        self.image_tree.column('filename', width=250, anchor=tk.W)
       
        # Configure headings
        self.image_tree.heading('selected', text='', anchor=tk.CENTER)
        self.image_tree.heading('filename', text='Filename', anchor=tk.W)
       
        self.image_tree.pack(fill=tk.BOTH, expand=True)
       
        # Bind treeview events
        self.image_tree.bind('<ButtonRelease-1>', self.on_tree_click)
        self.image_tree.bind('<Double-1>', self.on_tree_double_click)
       
        # Main display
        self.display_frame = tk.Frame(self.main_container)
        self.display_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
       
        self.canvas_frame = tk.Frame(self.display_frame)
        self.canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
       
        self.image_canvas = tk.Canvas(self.canvas_frame, highlightthickness=0, cursor="hand2")
        self.image_canvas.pack(fill=tk.BOTH, expand=True)
       
        self.image_canvas.bind("<ButtonPress-1>", self.on_canvas_click)
        self.image_canvas.bind("<B1-Motion>", self.on_pan_move)
       
        # Navigation
        self.nav_frame = tk.Frame(self.display_frame, height=60)
        self.nav_frame.pack(fill=tk.X, padx=10, pady=10)
        self.nav_frame.pack_propagate(False)
       
        self.prev_btn = tk.Button(self.nav_frame, text="⬅️ Previous", command=self.prev_image,
                 padx=20, pady=8, relief=tk.FLAT, cursor="hand2", font=("Segoe UI", 11))
        self.prev_btn.pack(side=tk.LEFT, padx=5)
       
        self.info_label = tk.Label(self.nav_frame, text="Select a folder to begin", font=("Segoe UI", 11))
        self.info_label.pack(side=tk.LEFT, expand=True)
       
        self.next_btn = tk.Button(self.nav_frame, text="Next ➡️", command=self.next_image,
                 padx=20, pady=8, relief=tk.FLAT, cursor="hand2", font=("Segoe UI", 11))
        self.next_btn.pack(side=tk.RIGHT, padx=5)
       
        self.update_nav_visibility()
       
    def on_tree_click(self, event):
        """Handle treeview clicks for selection toggling"""
        # Only handle selection in "selected" mode
        if self.view_mode.get() != "selected":
            return
           
        item = self.image_tree.identify_row(event.y)
        column = self.image_tree.identify_column(event.x)
       
        if item and column == '#1': # Clicked on the selection column
            # Toggle selection
            current_value = self.image_tree.set(item, 'selected')
            new_value = '☑' if current_value == '☐' else '☐'
            self.image_tree.set(item, 'selected', new_value)
           
            # Update the actual image data
            filename = self.image_tree.set(item, 'filename')
            img_data = next((img for img in self.images if img['name'] == filename), None)
            if img_data:
                img_data['selected'] = (new_value == '☑')
                self.refresh_filtered(preserve_grid=True)
   
    def on_tree_double_click(self, event):
        """Handle treeview double click to jump to image"""
        item = self.image_tree.identify_row(event.y)
        if item:
            filename = self.image_tree.set(item, 'filename')
            img_data = next((img for img in self.images if img['name'] == filename), None)
            if img_data and img_data in self.filtered_images:
                self.jump_to_image(self.images.index(img_data))
       
    def on_mouse_wheel(self, event):
        if not self.grid_mode:
            delta = event.delta
            if event.num == 4:
                delta = 120
            elif event.num == 5:
                delta = -120
            if delta > 0:
                self.zoom_in()
            elif delta < 0:
                self.zoom_out()
       
    def toggle_fullscreen(self):
        """Enter focus mode - hide all UI except image"""
        self.fullscreen_mode = not self.fullscreen_mode
       
        if self.fullscreen_mode:
            # Store sidebar state before hiding
            self.sidebar_was_visible = self.sidebar_visible
            # Hide all UI elements
            self.toolbar.pack_forget()
            self.nav_frame.pack_forget()
            self.sidebar.pack_forget()
            self.canvas_frame.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
            self.fullscreen_btn.config(text="🔎 Exit Focus")
           
            # Force update of canvas geometry before displaying image
            self.root.update_idletasks()
           
            # Display image after UI update
            self.root.after(50, lambda: [self.display_images(), self.show_arrows()])
        else:
            # Hide arrows first
            self.hide_arrows()
           
            # Restore all UI elements
            self.toolbar.pack(fill=tk.X, before=self.main_container)
            self.canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
            self.nav_frame.pack(fill=tk.X, padx=10, pady=10)
            # Restore sidebar only if it was visible before focus mode
            if self.sidebar_was_visible:
                self.sidebar.pack(side=tk.LEFT, fill=tk.Y, before=self.display_frame)
            self.fullscreen_btn.config(text="🔎 Focus Mode")
           
            # Force update of canvas geometry before displaying image
            self.root.update_idletasks()
           
            # Display image after UI update
            self.root.after(50, self.display_images)
   
    def show_arrows(self, event=None):
        """Show navigation arrows in focus mode"""
        if self.grid_mode or not self.fullscreen_mode or not self.filtered_images:
            return
       
        # Cancel previous timeout
        if self.arrow_timeout_id:
            self.root.after_cancel(self.arrow_timeout_id)
       
        # Remove existing arrows
        self.hide_arrows()
       
        # Get arrow colors from theme
        arrow_bg = self.get_color('arrow_bg')
        arrow_fg = self.get_color('arrow_fg')
       
        # Create semi-transparent arrows with better styling
        self.left_arrow = tk.Button(
            self.image_canvas, 
            text="◀", 
            command=self.prev_image,
            font=("Segoe UI", 20, "bold"), 
            bg=arrow_bg, 
            fg=arrow_fg,
            relief=tk.FLAT, 
            bd=0, 
            highlightthickness=1,
            highlightbackground=arrow_fg,
            cursor="hand2", 
            width=3, 
            height=2
        )
        self.left_arrow.place(relx=0.02, rely=0.5, anchor="w")
        
        self.right_arrow = tk.Button(
            self.image_canvas, 
            text="▶", 
            command=self.next_image,
            font=("Segoe UI", 20, "bold"), 
            bg=arrow_bg, 
            fg=arrow_fg,
            relief=tk.FLAT, 
            bd=0, 
            highlightthickness=1,
            highlightbackground=arrow_fg,
            cursor="hand2", 
            width=3, 
            height=2
        )
        self.right_arrow.place(relx=0.98, rely=0.5, anchor="e")
       
        # Auto-hide after 3 seconds
        self.arrow_timeout_id = self.root.after(3000, self.hide_arrows)
   
    def hide_arrows(self):
        if self.left_arrow:
            self.left_arrow.destroy()
            self.left_arrow = None
        if self.right_arrow:
            self.right_arrow.destroy()
            self.right_arrow = None
        if self.arrow_timeout_id:
            self.root.after_cancel(self.arrow_timeout_id)
            self.arrow_timeout_id = None
   
    def exit_fullscreen(self):
        """Exit focus mode with Escape key"""
        if self.fullscreen_mode:
            self.toggle_fullscreen()
   
    def on_canvas_click(self, event):
        if self.grid_mode:
            self.check_grid_click(event)
        elif self.zoom_level > 1.0:
            self.pan_start_x = event.x
            self.pan_start_y = event.y
            try:
                self.image_canvas.config(cursor="fleur")
            except:
                pass
        elif self.fullscreen_mode:
            # Show arrows when clicking in focus mode
            self.show_arrows()
   
    def on_pan_move(self, event):
        if not self.grid_mode and self.zoom_level > 1.0:
            dx = event.x - self.pan_start_x
            dy = event.y - self.pan_start_y
            self.image_offset_x += dx
            self.image_offset_y += dy
            self.pan_start_x = event.x
            self.pan_start_y = event.y
            self.display_images()
   
    def check_grid_click(self, event):
        if not self.grid_mode:
            return
       
        selected_images = [img for img in self.filtered_images if img['selected']]
        if not selected_images:
            return
       
        canvas_width = max(self.image_canvas.winfo_width(), 100)
        canvas_height = max(self.image_canvas.winfo_height(), 100)
       
        num_images = len(selected_images)
        cols = min(4, num_images)
        rows = (num_images + cols - 1) // cols
       
        cell_width = canvas_width // cols
        cell_height = canvas_height // rows
       
        col = event.x // cell_width
        row = event.y // cell_height
        clicked_idx = row * cols + col
       
        if 0 <= clicked_idx < len(selected_images):
            clicked_img = selected_images[clicked_idx]
            if clicked_img in self.filtered_images:
                self.current_index = self.filtered_images.index(clicked_img)
                self.grid_mode = False
                self.zoom_level = 1.0
                self.zoom_label.config(text="100%")
                self.image_offset_x = 0
                self.image_offset_y = 0
                self.update_nav_visibility()
                self.display_images()
       
    def apply_theme(self):
        bg = self.get_color('bg')
        sidebar_bg = self.get_color('sidebar')
        header_bg = self.get_color('header')
        text = self.get_color('text')
        button = self.get_color('button')
        button_text = self.get_color('button_text')
        entry_bg = self.get_color('entry_bg')
        tree_bg = self.get_color('tree_bg')
        tree_fg = self.get_color('tree_fg')
       
        self.root.config(bg=bg)
        self.toolbar.config(bg=header_bg)
        self.sidebar.config(bg=sidebar_bg)
        self.main_container.config(bg=bg)
        self.display_frame.config(bg=bg)
        self.canvas_frame.config(bg=bg)
        self.image_canvas.config(bg=bg)
        self.nav_frame.config(bg=header_bg)
       
        # Configure treeview style
        style = ttk.Style()
        if self.dark_mode:
            style.theme_use('clam')
            style.configure("Treeview",
                           background=tree_bg,
                           foreground=tree_fg,
                           fieldbackground=tree_bg,
                           borderwidth=0,
                           font=("Segoe UI", 11))
            style.configure("Treeview.Heading",
                           background=sidebar_bg,
                           foreground=tree_fg,
                           borderwidth=0,
                           font=("Segoe UI", 11, "bold"))
            style.map('Treeview', background=[('selected', button)])
        else:
            style.theme_use('clam')
            style.configure("Treeview",
                           background=tree_bg,
                           foreground=tree_fg,
                           fieldbackground=tree_bg,
                           borderwidth=0,
                           font=("Segoe UI", 11))
            style.configure("Treeview.Heading",
                           background=sidebar_bg,
                           foreground=tree_fg,
                           borderwidth=0,
                           font=("Segoe UI", 11, "bold"))
            style.map('Treeview', background=[('selected', button)])
       
        def update_widget(widget):
            try:
                widget_type = widget.winfo_class()
                if widget_type == 'Frame':
                    if widget == self.sidebar or widget.master == self.sidebar:
                        widget.config(bg=sidebar_bg)
                    elif widget in [self.toolbar, self.nav_frame]:
                        widget.config(bg=header_bg)
                    else:
                        widget.config(bg=bg)
                elif widget_type == 'Label':
                    parent = widget.master
                    if parent == self.sidebar or parent.master == self.sidebar:
                        widget.config(bg=sidebar_bg, fg=text)
                    elif parent in [self.toolbar, self.nav_frame]:
                        widget.config(bg=header_bg, fg=text)
                    else:
                        widget.config(bg=bg, fg=text)
                elif widget_type == 'Button':
                    widget.config(bg=button, fg=button_text, activebackground=button, activeforeground=button_text)
                elif widget_type == 'Entry':
                    widget.config(bg=entry_bg, fg=text, insertbackground=text)
                elif widget_type in ['Radiobutton', 'Checkbutton']:
                    widget.config(bg=sidebar_bg, fg=text, activebackground=sidebar_bg,
                                 activeforeground=text, selectcolor=sidebar_bg)
            except:
                pass
           
            for child in widget.winfo_children():
                update_widget(child)
       
        update_widget(self.root)
       
    def toggle_dark_mode(self):
        self.dark_mode = not self.dark_mode
        self.dark_mode_btn.config(text="🌞 Light Mode" if self.dark_mode else "🌜 Dark Mode")
        self.apply_theme()
        # Refresh arrows if in focus mode to match theme
        if self.fullscreen_mode:
            self.show_arrows()
       
    def toggle_sidebar(self):
        self.sidebar_visible = not self.sidebar_visible
       
        # Cancel any previous sidebar toggle or animation
        if self.sidebar_toggle_after_id:
            self.root.after_cancel(self.sidebar_toggle_after_id)
        if self.sidebar_animation_id:
            self.root.after_cancel(self.sidebar_animation_id)
       
        if self.sidebar_visible:
            self.sidebar_btn.config(text="⬅️ Hide Panel")
            self.smooth_show_sidebar()
        else:
            self.sidebar_btn.config(text="➡️ Show Panel")
            self.smooth_hide_sidebar()
   
    def smooth_hide_sidebar(self):
        """Smoothly hide the sidebar with animation"""
        current_width = self.sidebar.winfo_width()
        if current_width > 0:
            new_width = max(0, current_width - 40) # Reduce width by 40 pixels each step
            self.sidebar.config(width=new_width)
            self.sidebar.pack_propagate(False)
           
            if new_width > 0:
                # Continue animation
                self.sidebar_animation_id = self.root.after(10, self.smooth_hide_sidebar)
            else:
                # Animation complete, hide sidebar
                self.sidebar.pack_forget()
                self.sidebar_animation_id = None
               
                # Reset zoom and centering
                if not self.grid_mode and self.filtered_images:
                    self.zoom_level = 1.0
                    self.image_offset_x = 0
                    self.image_offset_y = 0
                    self.zoom_label.config(text="100%")
               
                # Update display
                self.display_images()
   
    def smooth_show_sidebar(self):
        """Smoothly show the sidebar with animation"""
        if not self.sidebar.winfo_ismapped():
            self.sidebar.pack(side=tk.LEFT, fill=tk.Y, before=self.display_frame)
            self.sidebar.config(width=0)
       
        current_width = self.sidebar.winfo_width()
        if current_width < self.sidebar_target_width:
            new_width = min(self.sidebar_target_width, current_width + 40) # Increase width by 40 pixels each step
            self.sidebar.config(width=new_width)
            self.sidebar.pack_propagate(False)
           
            if new_width < self.sidebar_target_width:
                # Continue animation
                self.sidebar_animation_id = self.root.after(10, self.smooth_show_sidebar)
            else:
                # Animation complete
                self.sidebar_animation_id = None
               
                # Reset zoom and centering
                if not self.grid_mode and self.filtered_images:
                    self.zoom_level = 1.0
                    self.image_offset_x = 0
                    self.image_offset_y = 0
                    self.zoom_label.config(text="100%")
               
                # Update display
                self.display_images()
   
    def update_nav_visibility(self):
        """Update navigation and back button visibility"""
        # Handle "Back to Grid" button
        if self.view_mode.get() == "selected" and not self.grid_mode and len(self.filtered_images) > 1:
            self.back_to_grid_btn.pack(side=tk.LEFT, padx=5, after=self.deselect_all_btn)
        else:
            self.back_to_grid_btn.pack_forget()
       
        # Handle navigation and zoom controls
        if self.grid_mode:
            self.prev_btn.pack_forget()
            self.next_btn.pack_forget()
            self.zoom_in_btn.pack_forget()
            self.zoom_out_btn.pack_forget()
            self.zoom_label.pack_forget()
            self.zoom_label_text.pack_forget()
        else:
            if not self.zoom_label_text.winfo_ismapped():
                self.zoom_label_text.pack(side=tk.RIGHT, padx=5)
                self.zoom_out_btn.pack(side=tk.RIGHT, padx=2)
                self.zoom_label.pack(side=tk.RIGHT, padx=2)
                self.zoom_in_btn.pack(side=tk.RIGHT, padx=2)
            if not self.prev_btn.winfo_ismapped():
                self.prev_btn.pack(side=tk.LEFT, padx=5)
                self.next_btn.pack(side=tk.RIGHT, padx=5)
       
    def zoom_in(self):
        if not self.grid_mode:
            self.zoom_level = min(5.0, self.zoom_level + 0.25)
            self.zoom_label.config(text=f"{int(self.zoom_level * 100)}%")
            self.display_images()
       
    def zoom_out(self):
        if not self.grid_mode:
            self.zoom_level = max(0.1, self.zoom_level - 0.25)
            self.zoom_label.config(text=f"{int(self.zoom_level * 100)}%")
            if self.zoom_level <= 1.0:
                self.image_offset_x = 0
                self.image_offset_y = 0
            self.display_images()
       
    def select_folder(self):
        folder = filedialog.askdirectory(title="Select Image Folder")
        if folder:
            self.image_folder = folder
            self.load_images()
           
    def natural_sort_key(self, s):
        """Natural sort key function for human-readable sorting"""
        return [int(text) if text.isdigit() else text.lower()
                for text in re.split(r'(\d+)', s)]
           
    def load_images(self):
        self.stop_loading = True
        if self.loading_thread and self.loading_thread.is_alive():
            self.loading_thread.join(timeout=1)
           
        self.stop_loading = False
        self.loading_thread = threading.Thread(target=self._load_images_thread)
        self.loading_thread.daemon = True
        self.loading_thread.start()
   
    def _load_images_thread(self):
        """Load images in a separate thread to prevent UI freezing"""
        self.images = []
        self.checkbutton_vars = []
        self.image_cache = {} # Clear cache when loading new folder
        supported_formats = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')
       
        try:
            files = [f for f in os.listdir(self.image_folder) if f.lower().endswith(supported_formats)]
           
            for i, file in enumerate(files):
                if self.stop_loading:
                    return
                   
                file_path = os.path.join(self.image_folder, file)
                timestamp = datetime.fromtimestamp(os.path.getmtime(file_path))
                self.images.append({
                    'path': file_path,
                    'name': file,
                    'selected': False,
                    'timestamp': timestamp
                })
               
                # Update UI in batches to prevent freezing
                if i % 10 == 0:
                    self.root.after(0, self._update_loading_progress, i, len(files))
           
            # Sort by name using natural sorting (1.png, 2.png, 10.png instead of 1.png, 10.png, 2.png)
            self.images.sort(key=lambda x: self.natural_sort_key(x['name']))
            self.root.after(0, self._finish_loading)
        except Exception as e:
            self.root.after(0, lambda: print(f"Error loading images: {e}"))
   
    def _update_loading_progress(self, current, total):
        """Update loading progress in the UI"""
        self.info_label.config(text=f"Loading... {current}/{total} images")
   
    def _finish_loading(self):
        """Finish loading and update UI"""
        self.current_index = 0
        self.zoom_level = 1.0
        self.zoom_label.config(text="100%")
        self.grid_mode = False
        self.update_image_list()
        self.update_view()
        self.info_label.config(text=f"Loaded {len(self.images)} images")
           
    def deselect_all(self):
        for img in self.images:
            img['selected'] = False
        self.update_image_list()
        self.update_view()
           
    def update_image_list(self):
        """Update the image list using Treeview with checkboxes"""
        # Clear existing items
        for item in self.image_tree.get_children():
            self.image_tree.delete(item)
       
        search_term = self.search_var.get().lower()
        display_images = [img for img in self.images if search_term in img['name'].lower()]
       
        # Show/hide checkbox column based on view mode
        if self.view_mode.get() == "selected":
            self.image_tree.column('selected', width=30, stretch=tk.NO)
            self.image_tree.heading('selected', text='☑')
        else:
            self.image_tree.column('selected', width=0, stretch=tk.NO)
            self.image_tree.heading('selected', text='')
       
        # Add images to treeview
        for img_data in display_images:
            checkbox = '☑' if img_data['selected'] else '☐'
            self.image_tree.insert('', 'end', values=(checkbox, img_data['name']))
   
    def toggle_selection(self, idx, var):
        self.images[idx]['selected'] = var.get()
        self.refresh_filtered(preserve_grid=True)
       
    def refresh_filtered(self, preserve_grid=False):
        search_term = self.search_var.get().lower()
        search_filtered = [img for img in self.images if search_term in img['name'].lower()]
       
        if self.view_mode.get() == "selected":
            self.filtered_images = [img for img in search_filtered if img['selected']]
            selected_count = len(self.filtered_images)
            if preserve_grid:
                if selected_count <= 1:
                    self.grid_mode = False
            else:
                self.grid_mode = selected_count > 1
        else:
            self.filtered_images = search_filtered
       
        if self.filtered_images and self.current_index >= len(self.filtered_images):
            self.current_index = max(0, len(self.filtered_images) - 1)
       
        self.update_nav_visibility()
        self.display_images()
       
    def update_view(self):
        search_term = self.search_var.get().lower()
        search_filtered = [img for img in self.images if search_term in img['name'].lower()]
       
        mode = self.view_mode.get()
        if mode == "all":
            self.filtered_images = search_filtered
            self.grid_mode = False
        elif mode == "selected":
            self.filtered_images = [img for img in search_filtered if img['selected']]
            self.grid_mode = len(self.filtered_images) > 1
       
        self.current_index = 0
        self.zoom_level = 1.0
        self.zoom_label.config(text="100%")
        self.image_offset_x = 0
        self.image_offset_y = 0
        self.update_image_list()
        self.update_nav_visibility()
        self.display_images()
       
    def on_search_change(self, *args):
        self.update_image_list()
        self.refresh_filtered(preserve_grid=True)
           
    def display_images(self):
        self.image_canvas.delete("all")
        self.photo_references = []
       
        if not self.filtered_images:
            text_color = self.get_color('text_secondary')
            self.root.update_idletasks()
            canvas_width = max(self.image_canvas.winfo_width(), 100)
            canvas_height = max(self.image_canvas.winfo_height(), 100)
            self.image_canvas.create_text(canvas_width // 2, canvas_height // 2,
                text="No images to display", font=("Segoe UI", 14), fill=text_color)
            self.info_label.config(text="No images")
            return
       
        selected_images = self.filtered_images
       
        if self.view_mode.get() == "selected" and len(selected_images) > 1 and self.grid_mode:
            self.display_grid(selected_images)
        else:
            self.grid_mode = False
            self.update_nav_visibility()
            self.display_single()
           
    def display_single(self):
        if not self.filtered_images:
            return
           
        img_data = self.filtered_images[self.current_index]
       
        try:
            # Use cached image if available
            if img_data['path'] in self.image_cache:
                pil_img = self.image_cache[img_data['path']]
            else:
                pil_img = Image.open(img_data['path'])
                # Cache the original image (resize happens later)
                self.image_cache[img_data['path']] = pil_img
           
            canvas_width = max(self.image_canvas.winfo_width(), 100)
            canvas_height = max(self.image_canvas.winfo_height(), 100)
           
            img_width, img_height = pil_img.size
           
            # Calculate scale to fit image properly
            if self.fullscreen_mode:
                # In focus mode, use the entire screen without padding
                scale_w = canvas_width / img_width
                scale_h = canvas_height / img_height
                scale = min(scale_w, scale_h) * self.zoom_level
            else:
                # In normal mode, use 95% of space with padding
                scale_w = (canvas_width * 0.95) / img_width
                scale_h = (canvas_height * 0.95) / img_height
                scale = min(scale_w, scale_h, 1.0) * self.zoom_level
           
            # Ensure minimum scale for very small zoom levels
            if scale * img_width < 10 or scale * img_height < 10:
                scale = max(10 / img_width, 10 / img_height, scale)
           
            new_width = max(1, int(img_width * scale))
            new_height = max(1, int(img_height * scale))
           
            # Only resize if necessary (performance optimization)
            if new_width != img_width or new_height != img_height:
                resized_img = pil_img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            else:
                resized_img = pil_img
               
            photo = ImageTk.PhotoImage(resized_img)
           
            # Calculate position (centered)
            x = canvas_width // 2 + self.image_offset_x
            y = canvas_height // 2 + self.image_offset_y
           
            self.image_canvas.create_image(x, y, image=photo)
            self.photo_references.append(photo)
           
            try:
                if self.zoom_level > 1.0:
                    self.image_canvas.config(cursor="fleur")
                else:
                    self.image_canvas.config(cursor="hand2")
            except:
                pass
           
            if not self.fullscreen_mode:
                time_str = img_data['timestamp'].strftime("%Y-%m-%d %H:%M:%S")
                info_text = f"{self.current_index + 1}/{len(self.filtered_images)} - {img_data['name']} - {time_str}"
                self.info_label.config(text=info_text)
           
        except Exception as e:
            print(f"Error displaying image: {e}")
           
    def display_grid(self, images):
        canvas_width = max(self.image_canvas.winfo_width(), 100)
        canvas_height = max(self.image_canvas.winfo_height(), 100)
       
        num_images = len(images)
        cols = min(4, num_images)
        rows = (num_images + cols - 1) // cols
       
        cell_width = canvas_width // cols
        cell_height = canvas_height // rows
       
        for i, img_data in enumerate(images):
            try:
                # Use cached image if available
                if img_data['path'] in self.image_cache:
                    pil_img = self.image_cache[img_data['path']]
                else:
                    pil_img = Image.open(img_data['path'])
                    self.image_cache[img_data['path']] = pil_img
                   
                col = i % cols
                row = i // cols
               
                img_w, img_h = pil_img.size
                scale_w = (cell_width * 0.98) / img_w
                scale_h = (cell_height * 0.98) / img_h
                scale = min(scale_w, scale_h)
               
                new_w = max(1, int(img_w * scale))
                new_h = max(1, int(img_h * scale))
               
                resized_img = pil_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(resized_img)
               
                x = col * cell_width + cell_width // 2
                y = row * cell_height + cell_height // 2
               
                self.image_canvas.create_image(x, y, image=photo)
                self.photo_references.append(photo)
               
            except Exception as e:
                print(f"Grid error: {e}")
       
        if not self.fullscreen_mode:
            self.info_label.config(text=f"Showing {num_images} selected images - Click any image to view full size")
       
    def next_image(self):
        if not self.grid_mode and self.filtered_images and self.current_index < len(self.filtered_images) - 1:
            self.current_index += 1
            self.zoom_level = 1.0
            self.zoom_label.config(text="100%")
            self.image_offset_x = 0
            self.image_offset_y = 0
            self.display_images()
            # Show arrows again if in focus mode
            if self.fullscreen_mode:
                self.root.after(100, self.show_arrows)
           
    def prev_image(self):
        if not self.grid_mode and self.filtered_images and self.current_index > 0:
            self.current_index -= 1
            self.zoom_level = 1.0
            self.zoom_label.config(text="100%")
            self.image_offset_x = 0
            self.image_offset_y = 0
            self.display_images()
            # Show arrows again if in focus mode
            if self.fullscreen_mode:
                self.show_arrows()
           
    def jump_to_image(self, idx):
        img_data = self.images[idx]
        if img_data in self.filtered_images:
            self.current_index = self.filtered_images.index(img_data)
            self.grid_mode = False
            self.zoom_level = 1.0
            self.zoom_label.config(text="100%")
            self.image_offset_x = 0
            self.image_offset_y = 0
            self.update_nav_visibility()
            self.display_images()
            
def main():
    """Main function to run the application properly"""
    # Create the main window
    root = tk.Tk()
   
    # Create the application
    app = ImageGallery(root)
   
    # Start the main loop
    root.mainloop()
    
if __name__ == "__main__":
    # Run as proper application
    main()