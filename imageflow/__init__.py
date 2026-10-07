"""ImageFlow - a fast image gallery for large folders.

Package layout:

    library.py   folder scanning, natural ordering, selection, list import/export
    thumbs.py    thumbnail decoding (priority workers, master thumbnails, disk cache)
    gui.py       the Tk desktop application
    web.py       the localhost browser viewer, sharing the same Library
    cli.py       command-line entry point used by ImageFlow.pyw
"""

__version__ = "2.0.0"
