# ImageFlow - Modern Image Viewer

A sleek, feature-rich image gallery application with dark mode, focus mode, and advanced image management capabilities.

## Features

- 🖼️ **Multi-Format Support** - View PNG, JPG, JPEG, GIF, BMP, and WebP images
- 🌓 **Dark/Light Themes** - Toggle between comfortable viewing modes
- 🔍 **Focus Mode** - Distraction-free fullscreen viewing with auto-hiding controls
- 🔎 **Zoom & Pan** - Zoom up to 500% with smooth panning
- 📊 **Grid View** - View multiple selected images simultaneously
- 🔍 **Search & Filter** - Quickly find images by filename
- ✅ **Selection System** - Mark and manage specific images
- ⌨️ **Keyboard Shortcuts** - Navigate efficiently with arrow keys, F11, and Escape

## Installation

### Requirements
- Python 3.7+
- Pillow (PIL)
- Natsort
- tkinter (included with Python on most systems)

### Setup

**Windows & Linux:**
```bash
pip install pillow natsort
```
```bash
# Debian/Ubuntu
sudo apt-get install python3-pillow
sudo apt-get install python3-natsort

# Fedora
sudo dnf install python3-pillow
sudo dnf install python3-natsort

# Arch
sudo pacman -S pillow
sudo pacman -S natsort
```

**Linux (if tkinter is missing):**
```bash
# Debian/Ubuntu
sudo apt-get install python3-tk

# Fedora
sudo dnf install python3-tkinter

# Arch
sudo pacman -S tk
```

## Usage

### Windows
Double-click `start.bat` to launch without a terminal window.

### Linux
Run directly:
```bash
chmod +x ./Start.sh
./Start.sh
```

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `←` / `→` | Navigate between images |
| `F11` | Toggle focus mode |
| `Escape` | Exit focus mode |
| `Mouse Wheel` | Zoom in/out |
| `Double Click` | Toggle focus mode |

## Controls

- **Select Folder** - Choose directory containing images
- **Search** - Filter images by filename
- **View Modes** - Switch between all images or selected only
- **Dark/Light Mode** - Toggle theme
- **Focus Mode** - Fullscreen viewing
- **Hide/Show Panel** - Toggle sidebar visibility
- **Zoom Controls** - Adjust zoom level (10% - 500%)
- **Grid View** - View multiple selected images (auto-enabled with 2+ selections)

## Tips

- In **Grid View**, click any image to view it fullscreen
- **Drag** images when zoomed to pan around
- Images are sorted naturally (1, 2, 10 instead of 1, 10, 2)
- Image cache improves performance when revisiting images
- Focus mode shows navigation arrows on mouse movement

## License

Apache-2.0 License - Feel free to use and modify
