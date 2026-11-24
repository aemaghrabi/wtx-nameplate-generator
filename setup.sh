#!/usr/bin/env bash
# Create the project venv and install dependencies.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
echo "Done. Run: .venv/bin/python make_nameplate.py --config example.json"
