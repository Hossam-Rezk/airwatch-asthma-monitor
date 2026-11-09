#!/usr/bin/env sh
# start.sh - sets everything up on the first run, then starts the dashboard (Linux / macOS).
#   ./start.sh            normal start
#   ./start.sh --demo     try it without a Pico
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  PY=$(command -v python3 || command -v python || true)
  if [ -z "$PY" ]; then
    echo "Python 3.9 or newer is needed: https://www.python.org/downloads/"
    exit 1
  fi
  echo "First start: preparing the environment. This takes a minute or two ..."
  "$PY" -m venv .venv
fi

if ! cmp -s requirements.txt .venv/requirements.installed; then
  echo "Installing Python packages ..."
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
  cp requirements.txt .venv/requirements.installed
fi

exec .venv/bin/python pc/app.py "$@"
