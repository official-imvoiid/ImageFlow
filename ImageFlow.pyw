#!/usr/bin/env python3
"""ImageFlow launcher. Double-click on Windows (pythonw) or run with python3."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from imageflow.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
