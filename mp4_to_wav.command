#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/mp4_to_wav.py"

normalize_input_path() {
  local raw="$1"
  python3 - "$raw" <<'PY'
import os
import shlex
import sys

value = sys.argv[1].strip()
if not value:
    print("")
    raise SystemExit(0)

try:
    parts = shlex.split(value)
    if parts:
        value = parts[0]
except ValueError:
    pass

print(os.path.expanduser(value))
PY
}

if [[ $# -ge 1 ]]; then
  INPUT_PATH="$1"
else
  echo "MP4 -> WAV"
  echo "Drag a .mp4 file into this window and press Enter."
  echo
  printf "File path: "
  IFS= read -r RAW_INPUT
  INPUT_PATH="$(normalize_input_path "$RAW_INPUT")"
fi

if [[ -z "${INPUT_PATH:-}" ]]; then
  echo "No file path entered."
  echo "Press Enter to close this window."
  read -r _
  exit 1
fi

if [[ ! -f "$INPUT_PATH" ]]; then
  echo "File not found: $INPUT_PATH"
  echo "Press Enter to close this window."
  read -r _
  exit 1
fi

ext="${INPUT_PATH##*.}"
ext_lc="$(echo "$ext" | tr '[:upper:]' '[:lower:]')"
if [[ "$ext_lc" != "mp4" ]]; then
  echo "Unsupported input (only .mp4): $INPUT_PATH"
  echo "Press Enter to close this window."
  read -r _
  exit 1
fi

echo
echo "Converting: $INPUT_PATH"
python3 "$PY_SCRIPT" "$INPUT_PATH"

echo
echo "Done. Press Enter to close this window."
read -r _
