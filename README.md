# ImageFlow — Modern Image Viewer

A fast, sleek image gallery built for browsing and triaging large folders.
Borderless focus mode, ghost-on-hover navigation, masonry collage layout,
and an export-selected-to-text workflow built in.

## Features

- 🖼️ **Multi-format support** — PNG, JPG, JPEG, GIF, BMP, WebP, TIFF
- 🧱 **Masonry / collage grid** — aspect-aware shortest-column layout, 3–6 cols
- 🌓 **Dark / light themes** — comfortable, readable palette in both
- 🔍 **Focus mode** — borderless, distraction-free single-image viewing
  - Ghost arrows: prev / next / exit fade in only when the cursor approaches
    the edge they live on
  - **Edge-drag resize** — drag any edge or corner with the cursor to resize
    the borderless window (no title bar required)
- 🔎 **Smart zoom & pan** — zoom 25% → 500%, drag to pan when zoomed
- ✅ **File-explorer-style selection**
  - Click filename → highlight in tree + scroll grid to that image
    (a red ring marks the image so you can spot it)
  - Double-click filename or thumbnail → open in single view
  - **Ctrl+click** = toggle one tick · **Shift+click** = range select
- 📝 **Export selected** — write picked filenames to a `.txt` file,
  one per line, with or without extensions
- 🔍 **Search & filter** — instant filename filter; "Selected only" view
- ⌨️ **Keyboard-first** — arrow keys, F11, Escape, Space, S, +/−
- ⚡ **Big-folder ready** — async thumbnail workers, LRU cache,
  pre-computed aspect ratios for the first 800 files (smooth on 10 000+
  image folders, even off USB on old laptops)

## Installation

### Requirements
- Python 3.7+
- Pillow (PIL)
- Natsort
- tkinter (bundled with Python on most systems)

### Setup

**Windows:**
```bash
pip install pillow natsort
```

**Linux:**
```bash
# Debian / Ubuntu
sudo apt-get install python3-pillow python3-natsort python3-tk

# Fedora
sudo dnf install python3-pillow python3-natsort python3-tkinter

# Arch
sudo pacman -S python-pillow python-natsort tk
```

## Usage

### Windows
Double-click `Start.bat` to launch (no console window).

### Linux
```bash
chmod +x ./Start.sh
./Start.sh
```

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `←` / `→` | Previous / next image (single view) |
| `Space` / `S` | Toggle selection of current image (single view) |
| `Double-click` | Open image in single view |
| `Mouse Wheel` | Scroll grid · zoom in single view |
| `Ctrl + Mouse Wheel` | Zoom (single view) |
| `Escape` | Cascade: single → grid, then exit focus mode |
| `F11` | Toggle real OS fullscreen (separate from Focus mode) |

## Mouse Behaviour

| Where | Action | Result |
|-------|--------|--------|
| Grid thumbnail | Single click | Sync sidebar tree to that image |
| Grid thumbnail | Double click | Open in single view |
| Grid thumbnail | Ctrl + click | Toggle ✓ tick on this image |
| Grid thumbnail | Shift + click | Range-select from last anchor → here |
| Sidebar filename | Single click | Scroll grid to that image (red ring marker) |
| Sidebar filename | Double click | Open in single view |
| Sidebar ✓ column | Single click | Toggle ✓ tick (with Shift / Ctrl modifiers) |
| Focus mode left edge | Cursor near edge | Prev arrow fades in |
| Focus mode right edge | Cursor near edge | Next arrow fades in |
| Focus mode top edge | Cursor near edge | Exit ✕ fades in |
| Focus mode any window edge | Cursor near border | Resize cursor — drag to resize |

## Controls

- **Open Folder** — choose a directory of images
- **Clear Selection** — deselect everything
- **Export List…** — save selected filenames to a `.txt` file
- **Include file extensions** (sidebar) — toggles `photo.jpg` vs `photo`
- **Search** — filter filenames as you type
- **View** — All Images / Selected Only
- **Columns** — 3 / 4 / 5 / 6
- **Focus** — borderless single-image mode (resizable by edge-drag)
- **Panel** — show / hide sidebar
- **☀ / ☾** — light / dark theme

## Tips

- Sorted naturally — `1, 2, 10` not `1, 10, 2`
- Aspect ratios for the first 800 images are pre-computed before first
  paint, so the masonry collage looks correct from the very first frame
- Thumbnails decode in a background thread — large folders feel instant
- The red focus ring after a tree click auto-clears after ~2 seconds
- In focus mode the OS title bar is gone, but you can still resize the
  window — just hover any edge or corner until the cursor changes

## License

Apache-2.0 — use, modify, and ship freely.
