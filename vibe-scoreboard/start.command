#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
if [[ ! -f app.py || ! -f tables.py || ! -f static/index.html ]]; then
  echo "Extract the ENTIRE ZIP first. app.py, tables.py and static must stay beside this script."
  read -r -p "Press Enter to close..."
  exit 1
fi
if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  python3 app.py "$@"
  scoreboard_exit=$?
else
  echo "Python 3.10+ is required. Install Python, then run this script again."
  scoreboard_exit=1
fi
read -r -p "Press Enter to close..."
exit "$scoreboard_exit"
