"""Command-line entry point.

    ImageFlow.pyw                      desktop window
    ImageFlow.pyw photo.jpg            open the folder, jump to that image
    ImageFlow.pyw --folder DIR         open a folder
    ImageFlow.pyw --folder DIR --txt L select the names listed in L (with or without extensions)
    ImageFlow.pyw --serve              desktop window plus the localhost web view (port 6001)
    ImageFlow.pyw --serve --no-gui     web view only (works without tkinter)
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .library import Library
from .thumbs import ThumbnailService


def parse_arguments(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog='ImageFlow', description='ImageFlow - Fast Image Viewer')
    p.add_argument('image', nargs='?', type=str, help='Image file to open')
    p.add_argument('--image', dest='image_flag', type=str, help='Image file to open')
    p.add_argument('--folder', type=str, help='Folder to open')
    p.add_argument('--txt', type=str, help='Text file with image names to select (extensions optional)')
    p.add_argument('--serve', action='store_true', help='Also serve the gallery on localhost')
    p.add_argument('--no-gui', action='store_true', help='With --serve: web view only, no window')
    p.add_argument('--host', default='127.0.0.1', help='Web view bind address (default 127.0.0.1)')
    p.add_argument('--port', type=int, default=None, help='Web view port (default 6001; busy or browser-blocked ports fall through to the next)')
    p.add_argument('--no-browser', action='store_true', help='Do not open the browser automatically')
    p.add_argument('--no-disk-cache', action='store_true', help='Keep thumbnails in memory only')
    p.add_argument('--cache-dir', type=str, default=None, help='Thumbnail cache directory')
    p.add_argument('--workers', type=int, default=None, help='Thumbnail worker threads')
    p.add_argument('--scale', type=float, default=None, help='UI scale override, e.g. 1.5 (default: from display DPI)')
    p.add_argument('--web-edit', action='store_true', help='Allow selecting images from the web view (view-only by default)')
    p.add_argument('--version', action='version', version=f'ImageFlow {__version__}')
    args = p.parse_args(argv)
    if args.image_flag:
        args.image = args.image_flag
    args.port_given = args.port is not None
    if args.port is None:
        args.port = 6001
    return args


def _initial_folder(args) -> str:
    if args.image and os.path.isfile(args.image):
        return os.path.dirname(os.path.abspath(args.image))
    if args.folder and os.path.isdir(args.folder):
        return os.path.abspath(args.folder)
    if args.txt and os.path.isfile(args.txt) and not args.folder:
        return os.path.dirname(os.path.abspath(args.txt))
    return ''


def main(argv=None) -> int:
    args = parse_arguments(argv)
    library = Library()
    service = ThumbnailService(workers=args.workers, disk_cache=not args.no_disk_cache,
                               cache_dir=args.cache_dir)

    try:
        import tkinter  # noqa: F401
        have_tk = True
    except Exception:
        have_tk = False

    if args.serve and (args.no_gui or not have_tk):
        from .web import serve_forever
        folder = _initial_folder(args)
        if folder:
            library.load(folder)
            service.load_aspect_index(folder, library.entries)
            service.read_aspects_now([e.path for e in library.entries[:48]], threads=8, budget=0.15)
            rest = [e.path for e in library.entries if e.path not in service.aspects]
            service.ensure_aspects(rest, on_progress=lambda d, t: (d >= t and t) and
                                   service.save_aspect_index(folder, library.entries))
            if args.txt and os.path.isfile(args.txt):
                library.import_file(args.txt)
        if not have_tk and not args.no_gui:
            print("tkinter is not available; running the web view only.", file=sys.stderr)
        serve_forever(library, service, args.host, args.port, open_browser=not args.no_browser,
                      allow_edit=args.web_edit)
        return 0

    if not have_tk:
        print("ImageFlow needs tkinter for the desktop window.\n"
              "  Debian/Ubuntu: sudo apt-get install python3-tk\n"
              "  Fedora:        sudo dnf install python3-tkinter\n"
              "Or run the browser version:  python ImageFlow.pyw --serve --no-gui", file=sys.stderr)
        return 1

    web_server = None
    if args.serve:
        from .web import start_server
        web_server = start_server(library, service, args.host, args.port, allow_edit=args.web_edit)
        print(f"ImageFlow web view: {web_server.url}")
        if not args.no_browser:
            import webbrowser
            try:
                webbrowser.open(web_server.url)
            except Exception:
                pass

    from .gui import run
    run(cli_args=args, library=library, service=service, web_host=args.host, web_port=args.port,
        web_server=web_server)
    return 0
