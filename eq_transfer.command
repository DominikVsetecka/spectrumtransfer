#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/eq_transfer.py"
REQ_FILE="$SCRIPT_DIR/requirements.txt"
VENV_DIR="$SCRIPT_DIR/.venv"
PY_BIN=""
REVERB_ARGS=()

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

lower_ext() {
  local path="$1"
  local ext="${path##*.}"
  echo "$ext" | tr '[:upper:]' '[:lower:]'
}

stamp() {
  date +"%Y%m%d_%H%M%S"
}

wait_and_exit() {
  local code="$1"
  echo
  echo "Fenster mit Enter schliessen."
  read -r _
  exit "$code"
}

ensure_python_env() {
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "Erstelle lokale Python-Umgebung in $VENV_DIR ..."
    if ! python3 -m venv "$VENV_DIR"; then
      echo "Konnte venv nicht erstellen."
      wait_and_exit 1
    fi
  fi

  PY_BIN="$VENV_DIR/bin/python"

  if ! "$PY_BIN" -c "import numpy, scipy" >/dev/null 2>&1; then
    echo "Installiere benoetigte Python-Pakete (numpy, scipy) ..."
    if ! "$PY_BIN" -m pip install -r "$REQ_FILE"; then
      echo "Paketinstallation fehlgeschlagen. Bitte Internet/Permissions pruefen."
      wait_and_exit 1
    fi
  fi
}

ensure_ffmpeg() {
  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg nicht gefunden. Bitte ffmpeg installieren."
    wait_and_exit 1
  fi
}

ask_reverb_match() {
  local ans=""
  echo
  echo "Raum-Matching (bessere Variante):"
  echo "  0) aus"
  echo "  1) auto (empfohlen: fuegt hinzu ODER reduziert)"
  echo "  2) add (nur Reverb hinzu)"
  echo "  3) remove (nur Dereverb)"
  printf "Auswahl [0/1/2/3] (default 1): "
  IFS= read -r ans
  ans="$(echo "$ans" | tr '[:upper:]' '[:lower:]')"

  case "$ans" in
    ""|"1"|"auto")
      REVERB_ARGS=(--match-reverb --reverb-mode auto --reverb-strength 0.55)
      echo "Raum-Matching: auto"
      ;;
    "2"|"add")
      REVERB_ARGS=(--match-reverb --reverb-mode add --reverb-strength 0.45)
      echo "Raum-Matching: add"
      ;;
    "3"|"remove"|"dereverb")
      REVERB_ARGS=(--match-reverb --reverb-mode remove --reverb-strength 0.75)
      echo "Raum-Matching: remove"
      ;;
    *)
      REVERB_ARGS=()
      echo "Raum-Matching: aus"
      ;;
  esac
}

extract_mp4_audio_wav() {
  local in_mp4="$1"
  local out_wav="$2"
  ffmpeg -y \
    -i "$in_mp4" \
    -vn \
    -map "0:a:0" \
    -acodec pcm_s16le \
    -ar 48000 \
    -ac 1 \
    "$out_wav"
}

remux_target_with_wav() {
  local in_mp4="$1"
  local in_wav="$2"
  local out_mp4="$3"
  ffmpeg -y \
    -i "$in_mp4" \
    -i "$in_wav" \
    -map "0:v:0?" \
    -map "1:a:0" \
    -c:v copy \
    -c:a aac \
    -b:a 192k \
    -movflags +faststart \
    -shortest \
    "$out_mp4"
}

if [[ $# -eq 2 ]]; then
  A="$1"
  B="$2"
else
  echo "EQ Transfer"
  echo "Gib 2 Dateipfade ein (Drag&Drop aus Finder ins Terminal)."
  echo "Modi:"
  echo "  1) desired.wav + target.wav"
  echo "  2) desired.txt + target.txt"
  echo "  3) curve.csv + target.wav"
  echo "  4) desired.wav + target.mp4"
  echo "  5) desired.mp4 + target.mp4 (Reihenfolge wichtig)"
  echo
  printf "Datei 1: "
  IFS= read -r RAW_A
  printf "Datei 2: "
  IFS= read -r RAW_B
  A="$(normalize_input_path "$RAW_A")"
  B="$(normalize_input_path "$RAW_B")"
fi

if [[ -z "${A:-}" || -z "${B:-}" ]]; then
  echo "Bitte zwei gueltige Dateipfade eingeben."
  wait_and_exit 1
fi

if [[ ! -f "$A" || ! -f "$B" ]]; then
  echo "Beide Inputs muessen existierende Dateien sein."
  wait_and_exit 1
fi

EA="$(lower_ext "$A")"
EB="$(lower_ext "$B")"
TS="$(stamp)"
ensure_python_env

# Mode 1: WAV + WAV => match (first desired, second target)
if [[ "$EA" == "wav" && "$EB" == "wav" ]]; then
  TARGET_DIR="$(cd "$(dirname "$B")" && pwd)"
  TARGET_BASE="$(basename "$B" .wav)"
  OUT_WAV="$TARGET_DIR/${TARGET_BASE}_matched_${TS}.wav"
  OUT_CSV="$TARGET_DIR/${TARGET_BASE}_curve_${TS}.csv"
  OUT_PRESET="$TARGET_DIR/${TARGET_BASE}_audacity_${TS}.txt"

  echo "Mode: match (desired.wav + target.wav)"
  ask_reverb_match
  if ! "$PY_BIN" "$PY_SCRIPT" match \
    --desired-wav "$A" \
    --target-wav "$B" \
    --out-wav "$OUT_WAV" \
    --curve-csv "$OUT_CSV" \
    --audacity-preset "$OUT_PRESET" \
    "${REVERB_ARGS[@]}"; then
    echo "Fehler beim EQ-Match."
    wait_and_exit 1
  fi

  echo "Output WAV: $OUT_WAV"
  echo "Curve CSV:  $OUT_CSV"
  echo "Preset TXT: $OUT_PRESET"
  wait_and_exit 0
fi

# Mode 4: desired WAV + target MP4 => extract audio, match, remux to new MP4
if [[ "$EA" == "wav" && "$EB" == "mp4" ]]; then
  DESIRED_WAV="$A"
  TARGET_MP4="$B"
elif [[ "$EA" == "mp4" && "$EB" == "wav" ]]; then
  DESIRED_WAV="$B"
  TARGET_MP4="$A"
else
  DESIRED_WAV=""
  TARGET_MP4=""
fi

  if [[ -n "$DESIRED_WAV" && -n "$TARGET_MP4" ]]; then
  ensure_ffmpeg
  TARGET_DIR="$(cd "$(dirname "$TARGET_MP4")" && pwd)"
  TARGET_BASE="$(basename "$TARGET_MP4" .mp4)"
  EXTRACT_WAV="$TARGET_DIR/${TARGET_BASE}_audio_original_${TS}.wav"
  MATCHED_WAV="$TARGET_DIR/${TARGET_BASE}_audio_matched_${TS}.wav"
  OUT_CSV="$TARGET_DIR/${TARGET_BASE}_curve_${TS}.csv"
  OUT_PRESET="$TARGET_DIR/${TARGET_BASE}_audacity_${TS}.txt"
  OUT_MP4="$TARGET_DIR/${TARGET_BASE}_matched_${TS}.mp4"

  echo "Mode: match+remux (desired.wav + target.mp4)"
  echo "Extrahiere Audio aus MP4 ..."
  if ! extract_mp4_audio_wav "$TARGET_MP4" "$EXTRACT_WAV"; then
    echo "Fehler beim Extrahieren der Audio-Spur aus MP4."
    wait_and_exit 1
  fi

  echo "Passe EQ auf desired.wav an ..."
  ask_reverb_match
  if ! "$PY_BIN" "$PY_SCRIPT" match \
    --desired-wav "$DESIRED_WAV" \
    --target-wav "$EXTRACT_WAV" \
    --out-wav "$MATCHED_WAV" \
    --curve-csv "$OUT_CSV" \
    --audacity-preset "$OUT_PRESET" \
    "${REVERB_ARGS[@]}"; then
    echo "Fehler beim EQ-Match der extrahierten Audio-Spur."
    wait_and_exit 1
  fi

  echo "Schreibe bearbeitete Audio-Spur zurueck in MP4 ..."
  if ! remux_target_with_wav "$TARGET_MP4" "$MATCHED_WAV" "$OUT_MP4"; then
    echo "Fehler beim Remux zur neuen MP4."
    wait_and_exit 1
  fi

  echo "Output MP4: $OUT_MP4"
  echo "Extract WAV: $EXTRACT_WAV"
  echo "Matched WAV: $MATCHED_WAV"
  echo "Curve CSV:   $OUT_CSV"
  echo "Preset TXT:  $OUT_PRESET"
  wait_and_exit 0
fi

# Mode 5: desired MP4 + target MP4 => extract both audios, match, remux target
if [[ "$EA" == "mp4" && "$EB" == "mp4" ]]; then
  ensure_ffmpeg
  DESIRED_MP4="$A"
  TARGET_MP4="$B"

  TARGET_DIR="$(cd "$(dirname "$TARGET_MP4")" && pwd)"
  TARGET_BASE="$(basename "$TARGET_MP4" .mp4)"
  DESIRED_BASE="$(basename "$DESIRED_MP4" .mp4)"

  DESIRED_WAV="$TARGET_DIR/${DESIRED_BASE}_desired_audio_${TS}.wav"
  EXTRACT_WAV="$TARGET_DIR/${TARGET_BASE}_audio_original_${TS}.wav"
  MATCHED_WAV="$TARGET_DIR/${TARGET_BASE}_audio_matched_${TS}.wav"
  OUT_CSV="$TARGET_DIR/${TARGET_BASE}_curve_${TS}.csv"
  OUT_PRESET="$TARGET_DIR/${TARGET_BASE}_audacity_${TS}.txt"
  OUT_MP4="$TARGET_DIR/${TARGET_BASE}_matched_${TS}.mp4"

  echo "Mode: match+remux (desired.mp4 + target.mp4)"
  echo "Extrahiere Audio aus desired.mp4 ..."
  if ! extract_mp4_audio_wav "$DESIRED_MP4" "$DESIRED_WAV"; then
    echo "Fehler beim Extrahieren der Audio-Spur aus desired.mp4."
    wait_and_exit 1
  fi

  echo "Extrahiere Audio aus target.mp4 ..."
  if ! extract_mp4_audio_wav "$TARGET_MP4" "$EXTRACT_WAV"; then
    echo "Fehler beim Extrahieren der Audio-Spur aus target.mp4."
    wait_and_exit 1
  fi

  echo "Passe target-Audio auf desired-Audio an ..."
  ask_reverb_match
  if ! "$PY_BIN" "$PY_SCRIPT" match \
    --desired-wav "$DESIRED_WAV" \
    --target-wav "$EXTRACT_WAV" \
    --out-wav "$MATCHED_WAV" \
    --curve-csv "$OUT_CSV" \
    --audacity-preset "$OUT_PRESET" \
    "${REVERB_ARGS[@]}"; then
    echo "Fehler beim EQ-Match der extrahierten Audio-Spuren."
    wait_and_exit 1
  fi

  echo "Schreibe bearbeitete Audio-Spur zurueck in target.mp4 ..."
  if ! remux_target_with_wav "$TARGET_MP4" "$MATCHED_WAV" "$OUT_MP4"; then
    echo "Fehler beim Remux zur neuen MP4."
    wait_and_exit 1
  fi

  echo "Output MP4:  $OUT_MP4"
  echo "Desired WAV: $DESIRED_WAV"
  echo "Target WAV:  $EXTRACT_WAV"
  echo "Matched WAV: $MATCHED_WAV"
  echo "Curve CSV:   $OUT_CSV"
  echo "Preset TXT:  $OUT_PRESET"
  wait_and_exit 0
fi

# Mode 2: TXT + TXT => curve (first desired, second target)
if [[ "$EA" == "txt" && "$EB" == "txt" ]]; then
  TARGET_DIR="$(cd "$(dirname "$B")" && pwd)"
  TARGET_BASE="$(basename "$B" .txt)"
  OUT_CSV="$TARGET_DIR/${TARGET_BASE}_curve_${TS}.csv"
  OUT_PRESET="$TARGET_DIR/${TARGET_BASE}_audacity_${TS}.txt"

  echo "Mode: curve (desired.txt + target.txt)"
  if ! "$PY_BIN" "$PY_SCRIPT" curve \
    --desired-spectrum "$A" \
    --target-spectrum "$B" \
    --curve-csv "$OUT_CSV" \
    --audacity-preset "$OUT_PRESET"; then
    echo "Fehler beim Erstellen der EQ-Kurve."
    wait_and_exit 1
  fi

  echo "Curve CSV:  $OUT_CSV"
  echo "Preset TXT: $OUT_PRESET"
  wait_and_exit 0
fi

# Mode 3: CSV + WAV => apply (order independent)
if [[ "$EA" == "csv" && "$EB" == "wav" ]]; then
  CURVE="$A"
  TARGET="$B"
elif [[ "$EA" == "wav" && "$EB" == "csv" ]]; then
  CURVE="$B"
  TARGET="$A"
else
  CURVE=""
  TARGET=""
fi

if [[ -n "$CURVE" && -n "$TARGET" ]]; then
  TARGET_DIR="$(cd "$(dirname "$TARGET")" && pwd)"
  TARGET_BASE="$(basename "$TARGET" .wav)"
  OUT_WAV="$TARGET_DIR/${TARGET_BASE}_applied_${TS}.wav"

  echo "Mode: apply (curve.csv + target.wav)"
  if ! "$PY_BIN" "$PY_SCRIPT" apply \
    --curve-csv "$CURVE" \
    --target-wav "$TARGET" \
    --out-wav "$OUT_WAV"; then
    echo "Fehler beim Anwenden der EQ-Kurve."
    wait_and_exit 1
  fi

  echo "Output WAV: $OUT_WAV"
  wait_and_exit 0
fi

echo "Unbekannte Kombination:"
echo " - $A"
echo " - $B"
echo "Erlaubt: wav+wav, txt+txt, csv+wav, wav+mp4, mp4+mp4"
wait_and_exit 1
