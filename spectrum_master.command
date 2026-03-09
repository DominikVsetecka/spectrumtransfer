#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/eq_transfer.py"
REQ_FILE="$SCRIPT_DIR/requirements.txt"
VENV_DIR="$SCRIPT_DIR/.venv"
PY_BIN=""
DYNAMICS_ARGS=()
DEESS_ARGS=()

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

wait_for_enter() {
  echo
  echo "Press Enter to continue."
  read -r _
}

ask_file_path() {
  local label="$1"
  local value=""
  printf "%s" "$label" >&2
  IFS= read -r value
  normalize_input_path "$value"
}

ensure_python_env() {
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "Creating local Python environment in $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
  fi

  PY_BIN="$VENV_DIR/bin/python"

  if ! "$PY_BIN" -c "import numpy, scipy" >/dev/null 2>&1; then
    echo "Installing required Python packages (numpy, scipy) ..."
    "$PY_BIN" -m pip install -r "$REQ_FILE"
  fi
}

ensure_ffmpeg() {
  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg not found. Please install ffmpeg."
    wait_for_enter
    return 1
  fi
  return 0
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

ask_dynamics_profile() {
  local ans=""
  echo
  echo "Auto level / anti-clipping:"
  echo "  0) off"
  echo "  1) gentle (target -6 dBFS, ceiling -3 dBFS)"
  echo "  2) strong (target -5 dBFS, ceiling -3 dBFS)"
  printf "Choose [0/1/2] (default 1): "
  IFS= read -r ans
  ans="$(echo "$ans" | tr '[:upper:]' '[:lower:]')"

  case "$ans" in
    "0"|"off")
      DYNAMICS_ARGS=()
      echo "Auto level: off"
      ;;
    "2"|"strong")
      DYNAMICS_ARGS=(
        --auto-level
        --target-dbfs -5.0
        --level-floor-dbfs -44.0
        --max-auto-boost-db 14.0
        --max-auto-cut-db 14.0
        --auto-attack-ms 22.0
        --auto-release-ms 320.0
        --compressor-threshold-dbfs -14.0
        --compressor-ratio 3.0
        --compressor-attack-ms 10.0
        --compressor-release-ms 140.0
        --limiter-ceiling-dbfs -3.0
        --dynamics-strength 1.0
      )
      echo "Auto level: strong"
      ;;
    *)
      DYNAMICS_ARGS=(
        --auto-level
        --target-dbfs -6.0
        --level-floor-dbfs -42.0
        --max-auto-boost-db 12.0
        --max-auto-cut-db 12.0
        --auto-attack-ms 35.0
        --auto-release-ms 450.0
        --compressor-threshold-dbfs -12.0
        --compressor-ratio 2.2
        --compressor-attack-ms 12.0
        --compressor-release-ms 120.0
        --limiter-ceiling-dbfs -3.0
        --dynamics-strength 1.0
      )
      echo "Auto level: gentle"
      ;;
  esac
}

ask_deesser_profile() {
  local ans=""
  echo
  echo "De-Esser (reduce harsh S / Z sounds):"
  echo "  0) off"
  echo "  1) gentle"
  echo "  2) strong"
  printf "Choose [0/1/2] (default 1): "
  IFS= read -r ans
  ans="$(echo "$ans" | tr '[:upper:]' '[:lower:]')"

  case "$ans" in
    "0"|"off")
      DEESS_ARGS=()
      echo "De-Esser: off"
      ;;
    "2"|"strong")
      DEESS_ARGS=(
        --de-ess
        --de-ess-low-hz 4200
        --de-ess-high-hz 10500
        --de-ess-threshold-dbfs -33.0
        --de-ess-ratio 4.2
        --de-ess-attack-ms 4.0
        --de-ess-release-ms 85.0
        --de-ess-strength 0.95
      )
      echo "De-Esser: strong"
      ;;
    *)
      DEESS_ARGS=(
        --de-ess
        --de-ess-low-hz 4500
        --de-ess-high-hz 10000
        --de-ess-threshold-dbfs -30.0
        --de-ess-ratio 3.0
        --de-ess-attack-ms 5.0
        --de-ess-release-ms 90.0
        --de-ess-strength 0.70
      )
      echo "De-Esser: gentle"
      ;;
  esac
}

run_match_command() {
  local desired_wav="$1"
  local target_wav="$2"
  local out_wav="$3"
  local curve_csv="$4"
  local audacity_preset="$5"
  local -a cmd

  cmd=(
    "$PY_BIN" "$PY_SCRIPT" match
    --desired-wav "$desired_wav"
    --target-wav "$target_wav"
    --out-wav "$out_wav"
    --curve-csv "$curve_csv"
    --audacity-preset "$audacity_preset"
  )

  if (( ${#DYNAMICS_ARGS[@]} > 0 )); then
    cmd+=("${DYNAMICS_ARGS[@]}")
  fi
  if (( ${#DEESS_ARGS[@]} > 0 )); then
    cmd+=("${DEESS_ARGS[@]}")
  fi

  "${cmd[@]}"
}

run_spectrum_master() {
  local a
  local b
  local ea
  local eb
  local ts

  echo
  echo "Enter files in this order:"
  echo "  File 1: desired/reference (wav/txt/csv/mp4)"
  echo "  File 2: target (wav/txt/mp4)"
  echo
  echo "Supported combos: wav+wav, txt+txt, csv+wav, wav+mp4, mp4+mp4"

  a="$(ask_file_path 'File 1: ')"
  b="$(ask_file_path 'File 2: ')"

  if [[ -z "$a" || -z "$b" ]]; then
    echo "Both file paths are required."
    wait_for_enter
    return
  fi

  if [[ ! -f "$a" || ! -f "$b" ]]; then
    echo "Both inputs must be existing files."
    wait_for_enter
    return
  fi

  ensure_python_env
  ea="$(lower_ext "$a")"
  eb="$(lower_ext "$b")"
  ts="$(stamp)"

  # Mode: WAV + WAV
  if [[ "$ea" == "wav" && "$eb" == "wav" ]]; then
    local target_dir target_base out_wav out_csv out_preset
    target_dir="$(cd "$(dirname "$b")" && pwd)"
    target_base="$(basename "$b" .wav)"
    out_wav="$target_dir/${target_base}_matched_${ts}.wav"
    out_csv="$target_dir/${target_base}_curve_${ts}.csv"
    out_preset="$target_dir/${target_base}_audacity_${ts}.txt"

    ask_dynamics_profile
    ask_deesser_profile

    if run_match_command "$a" "$b" "$out_wav" "$out_csv" "$out_preset"; then
      echo "Output WAV: $out_wav"
      echo "Curve CSV:  $out_csv"
      echo "Preset TXT: $out_preset"
    else
      echo "EQ processing failed."
    fi
    wait_for_enter
    return
  fi

  # Mode: TXT + TXT
  if [[ "$ea" == "txt" && "$eb" == "txt" ]]; then
    local target_dir target_base out_csv out_preset
    target_dir="$(cd "$(dirname "$b")" && pwd)"
    target_base="$(basename "$b" .txt)"
    out_csv="$target_dir/${target_base}_curve_${ts}.csv"
    out_preset="$target_dir/${target_base}_audacity_${ts}.txt"

    if "$PY_BIN" "$PY_SCRIPT" curve \
      --desired-spectrum "$a" \
      --target-spectrum "$b" \
      --curve-csv "$out_csv" \
      --audacity-preset "$out_preset"; then
      echo "Curve CSV:  $out_csv"
      echo "Preset TXT: $out_preset"
    else
      echo "Curve build failed."
    fi
    wait_for_enter
    return
  fi

  # Mode: CSV + WAV
  local curve="" target=""
  if [[ "$ea" == "csv" && "$eb" == "wav" ]]; then
    curve="$a"
    target="$b"
  elif [[ "$ea" == "wav" && "$eb" == "csv" ]]; then
    curve="$b"
    target="$a"
  fi

  if [[ -n "$curve" && -n "$target" ]]; then
    local target_dir target_base out_wav
    target_dir="$(cd "$(dirname "$target")" && pwd)"
    target_base="$(basename "$target" .wav)"
    out_wav="$target_dir/${target_base}_applied_${ts}.wav"

    ask_dynamics_profile
    ask_deesser_profile

    if "$PY_BIN" "$PY_SCRIPT" apply \
      --curve-csv "$curve" \
      --target-wav "$target" \
      --out-wav "$out_wav" \
      "${DYNAMICS_ARGS[@]}" \
      "${DEESS_ARGS[@]}"; then
      echo "Output WAV: $out_wav"
    else
      echo "Apply failed."
    fi
    wait_for_enter
    return
  fi

  # Mode: desired WAV + target MP4
  local desired_wav="" target_mp4=""
  if [[ "$ea" == "wav" && "$eb" == "mp4" ]]; then
    desired_wav="$a"
    target_mp4="$b"
  elif [[ "$ea" == "mp4" && "$eb" == "wav" ]]; then
    desired_wav="$b"
    target_mp4="$a"
  fi

  if [[ -n "$desired_wav" && -n "$target_mp4" ]]; then
    if ! ensure_ffmpeg; then
      return
    fi

    local target_dir target_base extract_wav matched_wav out_csv out_preset out_mp4
    target_dir="$(cd "$(dirname "$target_mp4")" && pwd)"
    target_base="$(basename "$target_mp4" .mp4)"
    extract_wav="$target_dir/${target_base}_audio_original_${ts}.wav"
    matched_wav="$target_dir/${target_base}_audio_matched_${ts}.wav"
    out_csv="$target_dir/${target_base}_curve_${ts}.csv"
    out_preset="$target_dir/${target_base}_audacity_${ts}.txt"
    out_mp4="$target_dir/${target_base}_matched_${ts}.mp4"

    echo "Extracting audio from target MP4 ..."
    extract_mp4_audio_wav "$target_mp4" "$extract_wav"

    ask_dynamics_profile
    ask_deesser_profile

    if run_match_command "$desired_wav" "$extract_wav" "$matched_wav" "$out_csv" "$out_preset"; then
      echo "Writing processed audio back to MP4 ..."
      remux_target_with_wav "$target_mp4" "$matched_wav" "$out_mp4"
      echo "Output MP4: $out_mp4"
    else
      echo "Match failed."
    fi
    wait_for_enter
    return
  fi

  # Mode: desired MP4 + target MP4
  if [[ "$ea" == "mp4" && "$eb" == "mp4" ]]; then
    if ! ensure_ffmpeg; then
      return
    fi

    local desired_mp4 target_mp4 target_dir target_base desired_base
    local desired_wav extract_wav matched_wav out_csv out_preset out_mp4

    desired_mp4="$a"
    target_mp4="$b"
    target_dir="$(cd "$(dirname "$target_mp4")" && pwd)"
    target_base="$(basename "$target_mp4" .mp4)"
    desired_base="$(basename "$desired_mp4" .mp4)"

    desired_wav="$target_dir/${desired_base}_desired_audio_${ts}.wav"
    extract_wav="$target_dir/${target_base}_audio_original_${ts}.wav"
    matched_wav="$target_dir/${target_base}_audio_matched_${ts}.wav"
    out_csv="$target_dir/${target_base}_curve_${ts}.csv"
    out_preset="$target_dir/${target_base}_audacity_${ts}.txt"
    out_mp4="$target_dir/${target_base}_matched_${ts}.mp4"

    echo "Extracting desired MP4 audio ..."
    extract_mp4_audio_wav "$desired_mp4" "$desired_wav"

    echo "Extracting target MP4 audio ..."
    extract_mp4_audio_wav "$target_mp4" "$extract_wav"

    ask_dynamics_profile
    ask_deesser_profile

    if run_match_command "$desired_wav" "$extract_wav" "$matched_wav" "$out_csv" "$out_preset"; then
      echo "Writing processed audio back to target MP4 ..."
      remux_target_with_wav "$target_mp4" "$matched_wav" "$out_mp4"
      echo "Output MP4: $out_mp4"
    else
      echo "Match failed."
    fi
    wait_for_enter
    return
  fi

  echo "Unknown file combination."
  echo "Allowed: wav+wav, txt+txt, csv+wav, wav+mp4, mp4+mp4"
  wait_for_enter
}

run_mp4_to_wav() {
  local in_mp4 out_wav

  if ! ensure_ffmpeg; then
    return
  fi

  echo
  in_mp4="$(ask_file_path 'MP4 file: ')"
  if [[ -z "$in_mp4" || ! -f "$in_mp4" ]]; then
    echo "Valid MP4 path required."
    wait_for_enter
    return
  fi

  if [[ "$(lower_ext "$in_mp4")" != "mp4" ]]; then
    echo "Input must be an .mp4 file."
    wait_for_enter
    return
  fi

  out_wav="${in_mp4%.*}.wav"
  if extract_mp4_audio_wav "$in_mp4" "$out_wav"; then
    echo "Saved WAV: $out_wav"
  else
    echo "MP4 -> WAV conversion failed."
  fi
  wait_for_enter
}

if [[ ! -f "$PY_SCRIPT" ]]; then
  echo "Missing $PY_SCRIPT"
  exit 1
fi

while true; do
  clear || true
  echo "Spectrum Transfer - Master Command"
  echo "----------------------------------"
  echo "1) Spectrum Master (EQ + Auto-Level + optional De-Esser)"
  echo "2) MP4 -> WAV"
  echo "3) Exit"
  echo
  printf "Choose [1/2/3]: "
  IFS= read -r choice

  case "${choice:-}" in
    1)
      run_spectrum_master
      ;;
    2)
      run_mp4_to_wav
      ;;
    3)
      echo "Bye."
      exit 0
      ;;
    *)
      echo "Invalid choice."
      wait_for_enter
      ;;
  esac
done
