#!/usr/bin/env bash
# macOS / Linux setup. On Windows use: setup.bat  (or setup.ps1 with Bypass)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is not installed or not in PATH." >&2
  exit 1
fi

if [ ! -d "venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi

# shellcheck source=/dev/null
source venv/bin/activate

echo "Upgrading pip..."
pip install --upgrade pip

echo "Installing dependencies..."
pip install -r requirements.txt

if [ ! -f ".env" ]; then
  echo ""
  echo "Note: .env not found. Copy .env.example to .env and set BRIDGE_API_KEY."
  cp -n .env.example .env 2>/dev/null || true
fi

echo ""
echo "Setup complete. Activate and start the bridge:"
echo "  source venv/bin/activate"
echo "  uvicorn main:app --host 0.0.0.0 --port 8787"
