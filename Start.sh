#!/bin/bash
# Launch the desktop viewer detached from this terminal. Extra arguments are
# passed through, e.g.  ./Start.sh --folder ~/Pictures
cd "$(dirname "$0")" || exit 1
nohup python3 ImageFlow.pyw "$@" > /dev/null 2>&1 &
