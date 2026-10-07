# ImageFlow — Modern Image Viewer

A fast image gallery for browsing and triaging large folders. Borderless focus
mode, ghost-on-hover navigation, masonry collage layout, and an
export/import-selected-to-text workflow built in. Runs as a desktop window
(Windows, Linux, macOS) and, with one flag, as a localhost web page you can
open in any browser.

## Features

- 🖼️ **Multi-format support** — PNG, JPG, JPEG, GIF, BMP, WebP, TIFF
- 🧱 **Masonry / collage grid** — aspect-aware shortest-column layout, 3–6 cols
- 🌓 **Dark / light themes**
- 🔍 **Focus mode** — borderless, distraction-free viewing with ghost arrows
  that fade in only when the cursor approaches an edge; edge-drag resize
- 🔎 **Smart zoom & pan** — 25% → 500%, drag to pan when zoomed
- ✅ **File-explorer-style selection** — click, Ctrl+click toggle, Shift+click range
- 📝 **Export selected** — filenames to a `.txt`, with or without extensions
- 📥 **Import selected** — load such a list back, **with or without extensions**
  (`photo`, `photo.jpg`, `PHOTO.JPG`, `photo.jpeg`, full paths, comma or
  line separated all work)
- 🔍 **Search & filter** — instant filename filter; "Selected only" view
- 🌐 **Localhost view** — browse in your browser at `http://localhost:6001`.
  It shows the desktop app's selection live and carries a permanent
  view-only notice; selecting, export and import happen in the desktop app
  (`--web-edit` allows selecting from the browser)
- ⌨️ **Keyboard-first** — arrow keys, F11, Escape, Space, S, +/−
- 🖥️ **Fits every screen** — the window sizes itself to the display, every
  control scales with the system DPI (100 %–400 %), the sidebar is draggable
  and remembered, the toolbar wraps on narrow windows
- 📱 **Responsive web view** — slide-in panel on phones and tablets, tap to
  open, long-press to select, swipe between images, pinch to zoom
- ⚡ **Big-folder ready** — see *Performance* below

## Installation

Requirements: Python 3.8+, Pillow, natsort (optional but recommended),
tkinter for the desktop window (bundled with Python on Windows and macOS).

```bash
# Windows
pip install pillow natsort

# Debian / Ubuntu
sudo apt-get install python3-pillow python3-pil.imagetk python3-natsort python3-tk
# Fedora
sudo dnf install python3-pillow python3-pillow-tk python3-natsort python3-tkinter
# Arch
sudo pacman -S python-pillow python-natsort tk
```

## Usage

| Platform | Desktop window | Desktop + browser view |
|---|---|---|
| Windows | double-click `Start.bat` | double-click `Serve.bat` |
| Linux / macOS | `./Start.sh` | `./Serve.sh` |

Command line (all platforms):

```bash
python ImageFlow.pyw                          # desktop window
python ImageFlow.pyw photo.jpg                # open its folder and jump to it
python ImageFlow.pyw --folder ~/Pictures      # open a folder
python ImageFlow.pyw --folder ~/Pictures --txt picks.txt   # and select the listed names
python ImageFlow.pyw --serve                  # window + http://localhost:6001
python ImageFlow.pyw --serve --no-gui --folder ~/Pictures  # browser only (no tkinter needed)
python ImageFlow.pyw --serve --host 0.0.0.0 --port 9000    # reachable from other devices
```

The **Web** toolbar button starts the browser view from a running window and
opens it; the **Port** box next to it sets the port (default 6001; if it is
busy, or one browsers refuse such as 6000, the next usable port is used and
shown). Selections made in the browser appear in the window and vice versa.

Other flags: `--no-browser`, `--no-disk-cache`, `--cache-dir DIR`, `--workers N`,
`--scale 1.5` (UI scale override; normally read from the display DPI, or the
`IMAGEFLOW_SCALE` environment variable), `--web-edit` (let the browser change
the selection).

Window size and position, theme, columns, sidebar width and the export
extension setting are remembered in `settings.json` next to the thumbnail cache.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `←` / `→` | Previous / next image (single view) |
| `Space` / `S` | Toggle selection of current image (single view) |
| `+` / `−` | Zoom (single view; also mouse wheel) |
| `Double-click` | Open image in single view |
| `Mouse Wheel` | Scroll grid · zoom in single view |
| `Escape` | Cascade: single → grid, then exit focus mode |
| `F11` | Real OS fullscreen (separate from Focus mode) |

## Mouse Behaviour

| Where | Action | Result |
|-------|--------|--------|
| Grid thumbnail | Single click | Sync sidebar list to that image |
| Grid thumbnail | Double click | Open in single view |
| Grid thumbnail | Ctrl + click | Toggle ✓ on this image |
| Grid thumbnail | Shift + click | Range-select from last anchor → here |
| Sidebar filename | Single click | Scroll grid to that image (red ring) |
| Sidebar filename | Double click | Open in single view |
| Sidebar ✓ column | Single click | Toggle ✓ (Shift for range) |
| Focus mode edges | Cursor near edge | Prev / next / exit controls fade in |
| Focus mode window edge | Drag | Resize the borderless window |

## Controls

- **Open Folder** · **Clear Selection** · **Export** · **Import** · **Web**
- **Zoom − / +** in the bottom bar while viewing a single image
- **Include file extensions** (sidebar) — `photo.jpg` vs `photo` on export
- **Search** · **View** (All / Selected Only) · **Columns** (3–6)
- **Focus** · **Panel** · **☀ / ☾**

## Performance

What makes it feel like a native gallery:

- **Visible-first thumbnails.** Decoding is scheduled by what is on screen;
  tiles you scrolled past are dropped from the queue instead of decoded.
- **Decode once.** Each image is decoded to a master thumbnail, and every
  column width, window size or column count is produced from that, never
  from the file again. JPEGs use the codec's reduced-size decode.
- **Thumbnail disk cache** in your user cache directory
  (`%LOCALAPPDATA%\ImageFlow`, `~/.cache/imageflow`), capped at 1 GB, keyed
  by path, size and modification time, plus a per-folder aspect index.
  Reopening a folder is instant.
- **Virtualised grid and file list.** Only items near the viewport exist;
  scrolling, selecting or a thumbnail arriving updates items in place.
  Hover and click use a per-column binary search, so a 20 000-image folder
  behaves like a 20-image one.
- **Opening a folder never waits for file reads.** The grid paints right
  after the directory listing; aspect ratios are read first for what is on
  screen (at most 0.15 s before the first paint), the rest stream in with the
  view anchored, and every ratio learnt is remembered per folder so the next
  open lays out perfectly with zero file reads.
- **Fast single view.** A cached thumbnail appears instantly, the real image
  decodes on a worker at the size the screen needs, neighbours are
  pre-decoded, and when zoomed only the visible crop is rendered, so 500 % on
  a 40 MP photo is smooth and panning moves the existing frame.
- **EXIF orientation** honoured everywhere, so phone photos are upright.

## Layout

```
ImageFlow.pyw        launcher
imageflow/
  library.py         folder scanning, natural order, selection, list import/export
  thumbs.py          thumbnail workers, master thumbnails, disk cache
  gui.py             the Tk desktop window
  web.py             the localhost server
  static/index.html  the browser interface
  cli.py             command line
tests/               pytest suite (GUI tests run under a display, e.g. xvfb-run)
```

## License

Apache-2.0 — use, modify, and ship freely.
