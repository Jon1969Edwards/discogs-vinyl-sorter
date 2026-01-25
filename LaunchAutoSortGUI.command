#!/usr/bin/env zsh
set -euo pipefail

cd "$(dirname "$0")"

python_bin=""
if [ -x ".venv/bin/python" ]; then
  python_bin=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  python_bin="python3"
elif command -v python >/dev/null 2>&1; then
  python_bin="python"
else
  echo "ERROR: Could not find Python. Install Python 3.9+ and/or create .venv." >&2
  read -r "?Press Enter to close..."
  exit 1
fi

"$python_bin" autosort_gui.py || {
  echo ""
  echo "ERROR: Auto-Sort GUI failed to start." >&2
  echo "If you see a Tk error, install a Python build with Tk support (python.org installer)" >&2
  echo "and recreate your .venv." >&2
  read -r "?Press Enter to close..."
  exit 1
}
