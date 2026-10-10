#!/usr/bin/env bash
# Linux / macOS: first run creates a virtual environment, then launches FAI Studio.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -r requirements.txt || exit 1; .venv/bin/pip install -r requirements-ocr.txt || echo "OCR optional - skipped"
fi
exec .venv/bin/python run.py
