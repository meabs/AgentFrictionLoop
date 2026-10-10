#!/bin/sh
# macOS / Linux / Git Bash: forwards all options to install.py (see its --help).
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for py in python3 python; do
  command -v "$py" >/dev/null 2>&1 && exec "$py" "$here/install.py" "$@"
done
echo "python 3 is required" >&2; exit 1
