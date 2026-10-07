#!/bin/bash
# Desktop viewer plus the localhost web view (http://localhost:6001).
# Use  ./Serve.sh --no-gui --folder ~/Pictures  for the browser version only.
cd "$(dirname "$0")" || exit 1
exec python3 ImageFlow.pyw --serve "$@"
