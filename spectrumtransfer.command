#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/spectrumtransfer.py"
REQ_FILE="$SCRIPT_DIR/requirements.txt"
VENV_DIR="$SCRIPT_DIR/.venv"
PY_BIN=""
DYNAMICS_ARGS=()
DEESS_ARGS=()
WHINE_ARGS=()
SPECTRUM_REF=""
SPECTRUM_REF_WAV=""
SPECTRUM_ARGS=()
CLARITY_ARGS=()
SECOND_EQ_ARGS=()
PEAK_NORMALIZE_ARGS=()
PEAK_CEILING_ARGS=()

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

file_stem() {
  local name
  name="$(basename "$1")"
  echo "${name%.*}"
}

canonical_path() {
  local path="$1"
  local dir
  local name
  dir="$(cd "$(dirname "$path")" && pwd)"
  name="$(basename "$path")"
  echo "$dir/$name"
}

same_file_path() {
  [[ "$(canonical_path "$1")" == "$(canonical_path "$2")" ]]
}

is_generated_artifact() {
  local name
  name="$(basename "$1")"
  case "$name" in
    *_matched_*|*_applied_*|*_fixed_*|*_whine_reduced_*|*_audio_original_*|*_audio_matched_*|*_audio_fixed_*|*_audio_whine_reduced_*|*_desired_audio_*|*_curve_*|*_audacity_*)
      return 0
      ;;
  esac
  return 1
}

folder_contains_ext() {
  local folder="$1"
  local ext="$2"
  [[ -n "$(find "$folder" -maxdepth 1 -type f -iname "*.$ext" -print -quit)" ]]
}

stamp() {
  date +"%Y%m%d_%H%M%S"
}

wait_for_enter() {
  echo
  echo "Press Enter to continue."
  read -r _
}

print_failed_files() {
  local file

  if [[ "$#" -eq 0 ]]; then
    return
  fi

  echo
  echo "Failed files ($#):"
  for file in "$@"; do
    echo "  - $file"
  done
}

ask_retry_failed_files() {
  local ans

  echo
  echo "Retry failed files now?"
  echo "  1) yes"
  echo "  2) no"
  printf "Choose [1/2] (default 2): "
  IFS= read -r ans
  ans="$(echo "$ans" | tr '[:upper:]' '[:lower:]')"

  case "$ans" in
    1|y|yes|j|ja)
      return 0
      ;;
  esac
  return 1
}

ask_on_off() {
  local label="$1"
  local ans

  echo "$label:"
  echo "  1) on"
  echo "  2) off"
  printf "Choose [1/2] (default 2): "
  IFS= read -r ans
  ans="$(echo "$ans" | tr '[:upper:]' '[:lower:]')"

  case "$ans" in
    1|on|y|yes|j|ja)
      return 0
      ;;
  esac
  return 1
}

ask_file_path() {
  local label="$1"
  local value=""
  printf "%s" "$label" >&2
  IFS= read -r value
  normalize_input_path "$value"
}

ensure_python_version() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Please install Python 3.9 or newer."
    wait_for_enter
    return 1
  fi

  if ! python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
  then
    echo "Python 3.9+ is required."
    echo -n "Detected: "
    python3 --version
    wait_for_enter
    return 1
  fi

  return 0
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
  ffmpeg -nostdin -y \
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
  ffmpeg -nostdin -y \
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
  echo "  1) gentle smooth (target -6 dBFS, ceiling -3 dBFS)"
  echo "  2) vocal fast (less pumping, target -7 dBFS, ceiling -3 dBFS)"
  echo "  3) strong (target -5 dBFS, ceiling -3 dBFS)"
  printf "Choose [0/1/2/3] (default 2): "
  IFS= read -r ans
  ans="$(echo "$ans" | tr '[:upper:]' '[:lower:]')"

  case "$ans" in
    "0"|"off")
      DYNAMICS_ARGS=()
      echo "Auto level: off"
      ;;
    "3"|"strong")
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
    "1"|"gentle")
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
    *)
      DYNAMICS_ARGS=(
        --auto-level
        --target-dbfs -7.0
        --level-floor-dbfs -36.0
        --max-auto-boost-db 7.0
        --max-auto-cut-db 10.0
        --auto-attack-ms 18.0
        --auto-release-ms 180.0
        --compressor-threshold-dbfs -14.0
        --compressor-ratio 2.0
        --compressor-attack-ms 8.0
        --compressor-release-ms 90.0
        --limiter-ceiling-dbfs -3.0
        --dynamics-strength 0.75
      )
      echo "Auto level: vocal fast"
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

ask_whine_profile() {
  local ans=""
  echo
  echo "Electrical whine reduction (high cut + optional notch filters):"
  echo "  0) off"
  echo "  1) gentle (high cut at 17.4 kHz + 1 notch)"
  echo "  2) strong (high cut at 17.4 kHz + 3 notches)"
  printf "Choose [0/1/2] (default 2): "
  IFS= read -r ans
  ans="$(echo "$ans" | tr '[:upper:]' '[:lower:]')"

  case "$ans" in
    "0"|"off")
      WHINE_ARGS=()
      echo "Whine reduction: off"
      ;;
    "1"|"gentle")
      WHINE_ARGS=(
        --whine-high-cut
        --whine-high-cut-freq-hz 17400
        --whine-high-cut-gain-db -30.0
        --whine-high-cut-q 1.2
        --whine-notch-hz 15600
        --whine-notch-gain-db -14.0
        --whine-notch-q 9.0
      )
      echo "Whine reduction: gentle"
      ;;
    *)
      WHINE_ARGS=(
        --whine-high-cut
        --whine-high-cut-freq-hz 17400
        --whine-high-cut-gain-db -30.0
        --whine-high-cut-q 1.2
        --whine-notch-hz 15600 16800 18100
        --whine-notch-gain-db -18.0
        --whine-notch-q 10.0
      )
      echo "Whine reduction: strong"
      ;;
  esac
}

ask_spectrum_profile() {
  local ref ext

  SPECTRUM_REF=""
  SPECTRUM_REF_WAV=""
  SPECTRUM_ARGS=()

  echo
  if ! ask_on_off "Spectrum Master (EQ transfer / curve apply)"; then
    echo "Spectrum Master: off"
    return
  fi

  ref="$(ask_file_path 'File 1 desired/reference (wav/txt/csv/mp4): ')"
  if [[ -z "$ref" || ! -f "$ref" ]]; then
    echo "Spectrum Master: valid reference file required, disabling."
    return
  fi

  ext="$(lower_ext "$ref")"
  case "$ext" in
    wav|csv|mp4)
      SPECTRUM_REF="$ref"
      echo "Spectrum Master: on"
      ;;
    txt)
      echo "TXT spectrum references are only supported by the old curve-only flow, not this audio pipeline."
      echo "Spectrum Master: off"
      ;;
    *)
      echo "Spectrum Master supports .wav, .csv, or .mp4 here."
      echo "Spectrum Master: off"
      ;;
  esac
}

ask_peak_normalizer_profile() {
  PEAK_NORMALIZE_ARGS=()

  echo
  if ask_on_off "Peak normalizer to -6 dBFS"; then
    PEAK_NORMALIZE_ARGS=(--peak-normalize --peak-normalize-dbfs -6.0)
    echo "Peak normalizer: -6 dBFS"
  else
    echo "Peak normalizer: off"
  fi
}

ask_voice_clarity_profile() {
  local ans=""
  CLARITY_ARGS=()

  echo
  echo "Voice Clarity (presence + air for clearer speech):"
  echo "  0) off"
  echo "  1) gentle"
  echo "  2) clear"
  printf "Choose [0/1/2] (default 1): "
  IFS= read -r ans
  ans="$(echo "$ans" | tr '[:upper:]' '[:lower:]')"

  case "$ans" in
    "0"|"off")
      CLARITY_ARGS=()
      echo "Voice Clarity: off"
      ;;
    "2"|"clear")
      CLARITY_ARGS=(--voice-clarity clear)
      echo "Voice Clarity: clear"
      ;;
    *)
      CLARITY_ARGS=(--voice-clarity gentle)
      echo "Voice Clarity: gentle"
      ;;
  esac
}

ask_second_eq_profile() {
  local ans=""
  SECOND_EQ_ARGS=()

  echo
  echo "Second Step EQ (Audacity-style clarity curve):"
  echo "  0) off"
  echo "  1) on"
  printf "Choose [0/1] (default 0): "
  IFS= read -r ans
  ans="$(echo "$ans" | tr '[:upper:]' '[:lower:]')"

  case "$ans" in
    "1"|"on")
      SECOND_EQ_ARGS=(--second-step-eq)
      echo "Second Step EQ: on"
      ;;
    *)
      SECOND_EQ_ARGS=()
      echo "Second Step EQ: off"
      ;;
  esac
}

ask_peak_ceiling_profile() {
  PEAK_CEILING_ARGS=()

  echo
  if ask_on_off "Peak ceiling at -6 dBFS"; then
    PEAK_CEILING_ARGS=(--peak-ceiling --peak-ceiling-dbfs -6.0)
    echo "Peak ceiling: -6 dBFS"
  else
    echo "Peak ceiling: off"
  fi
}

set_fast_pipeline_defaults() {
  DYNAMICS_ARGS=(
    --auto-level
    --target-dbfs -7.0
    --level-floor-dbfs -36.0
    --max-auto-boost-db 7.0
    --max-auto-cut-db 10.0
    --auto-attack-ms 18.0
    --auto-release-ms 180.0
    --compressor-threshold-dbfs -14.0
    --compressor-ratio 2.0
    --compressor-attack-ms 8.0
    --compressor-release-ms 90.0
    --limiter-ceiling-dbfs -3.0
    --dynamics-strength 0.75
  )
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
  SPECTRUM_REF=""
  SPECTRUM_REF_WAV=""
  SPECTRUM_ARGS=()
  CLARITY_ARGS=(--voice-clarity gentle)
  SECOND_EQ_ARGS=()
  PEAK_NORMALIZE_ARGS=(--peak-normalize --peak-normalize-dbfs -6.0)
  PEAK_CEILING_ARGS=(--peak-ceiling --peak-ceiling-dbfs -6.0)
  WHINE_ARGS=(
    --whine-high-cut
    --whine-high-cut-freq-hz 17400
    --whine-high-cut-gain-db -30.0
    --whine-high-cut-q 1.2
    --whine-notch-hz 15600
    --whine-notch-gain-db -14.0
    --whine-notch-q 9.0
  )

  echo
  echo "Fast settings:"
  echo "  Auto level: vocal fast"
  echo "  Spectrum Master: off"
  echo "  De-Esser: gentle"
  echo "  Voice Clarity: gentle"
  echo "  Second Step EQ: off"
  echo "  Peak normalizer: -6 dBFS"
  echo "  Peak ceiling: -6 dBFS"
  echo "  Whine reduction: gentle"
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
  if (( ${#WHINE_ARGS[@]} > 0 )); then
    cmd+=("${WHINE_ARGS[@]}")
  fi

  "${cmd[@]}"
}

process_match_wav_target() {
  local desired_wav="$1"
  local target_wav="$2"
  local ts="$3"
  local target_dir target_base out_wav out_csv out_preset

  target_dir="$(cd "$(dirname "$target_wav")" && pwd)"
  target_base="$(file_stem "$target_wav")"
  out_wav="$target_dir/${target_base}_matched_${ts}.wav"
  out_csv="$target_dir/${target_base}_curve_${ts}.csv"
  out_preset="$target_dir/${target_base}_audacity_${ts}.txt"

  echo
  echo "Processing WAV: $target_wav"
  if run_match_command "$desired_wav" "$target_wav" "$out_wav" "$out_csv" "$out_preset"; then
    echo "Output WAV: $out_wav"
    echo "Curve CSV:  $out_csv"
    echo "Preset TXT: $out_preset"
    return 0
  fi

  echo "EQ processing failed: $target_wav"
  return 1
}

process_curve_txt_target() {
  local desired_txt="$1"
  local target_txt="$2"
  local ts="$3"
  local target_dir target_base out_csv out_preset

  target_dir="$(cd "$(dirname "$target_txt")" && pwd)"
  target_base="$(file_stem "$target_txt")"
  out_csv="$target_dir/${target_base}_curve_${ts}.csv"
  out_preset="$target_dir/${target_base}_audacity_${ts}.txt"

  echo
  echo "Processing TXT spectrum: $target_txt"
  if "$PY_BIN" "$PY_SCRIPT" curve \
    --desired-spectrum "$desired_txt" \
    --target-spectrum "$target_txt" \
    --curve-csv "$out_csv" \
    --audacity-preset "$out_preset" \
    "${WHINE_ARGS[@]}"; then
    echo "Curve CSV:  $out_csv"
    echo "Preset TXT: $out_preset"
    return 0
  fi

  echo "Curve build failed: $target_txt"
  return 1
}

process_apply_curve_target() {
  local curve_csv="$1"
  local target_wav="$2"
  local ts="$3"
  local target_dir target_base out_wav

  target_dir="$(cd "$(dirname "$target_wav")" && pwd)"
  target_base="$(file_stem "$target_wav")"
  out_wav="$target_dir/${target_base}_applied_${ts}.wav"

  echo
  echo "Applying curve to WAV: $target_wav"
  if "$PY_BIN" "$PY_SCRIPT" apply \
    --curve-csv "$curve_csv" \
    --target-wav "$target_wav" \
    --out-wav "$out_wav" \
    "${DYNAMICS_ARGS[@]}" \
    "${DEESS_ARGS[@]}" \
    "${WHINE_ARGS[@]}"; then
    echo "Output WAV: $out_wav"
    return 0
  fi

  echo "Apply failed: $target_wav"
  return 1
}

process_desired_wav_target_mp4() {
  local desired_wav="$1"
  local target_mp4="$2"
  local ts="$3"
  local target_dir target_base extract_wav matched_wav out_csv out_preset out_mp4

  target_dir="$(cd "$(dirname "$target_mp4")" && pwd)"
  target_base="$(file_stem "$target_mp4")"
  extract_wav="$target_dir/${target_base}_audio_original_${ts}.wav"
  matched_wav="$target_dir/${target_base}_audio_matched_${ts}.wav"
  out_csv="$target_dir/${target_base}_curve_${ts}.csv"
  out_preset="$target_dir/${target_base}_audacity_${ts}.txt"
  out_mp4="$target_dir/${target_base}_matched_${ts}.mp4"

  echo
  echo "Processing MP4: $target_mp4"
  echo "Extracting audio from target MP4 ..."
  if ! extract_mp4_audio_wav "$target_mp4" "$extract_wav"; then
    echo "Audio extraction failed: $target_mp4"
    return 1
  fi

  if run_match_command "$desired_wav" "$extract_wav" "$matched_wav" "$out_csv" "$out_preset"; then
    echo "Writing processed audio back to MP4 ..."
    if remux_target_with_wav "$target_mp4" "$matched_wav" "$out_mp4"; then
      rm -f "$extract_wav"
      echo "Output MP4: $out_mp4"
      return 0
    fi
    echo "MP4 remux failed: $target_mp4"
    return 1
  fi

  echo "Match failed: $target_mp4"
  return 1
}

process_whine_wav_target() {
  local target_wav="$1"
  local ts="$2"
  local target_dir base out_wav

  target_dir="$(cd "$(dirname "$target_wav")" && pwd)"
  base="$(file_stem "$target_wav")"
  out_wav="$target_dir/${base}_whine_reduced_${ts}.wav"

  echo
  echo "Processing WAV: $target_wav"
  if "$PY_BIN" "$PY_SCRIPT" whine \
    --target-wav "$target_wav" \
    --out-wav "$out_wav" \
    "${WHINE_ARGS[@]}"; then
    echo "Output WAV: $out_wav"
    return 0
  fi

  echo "Whine-only processing failed: $target_wav"
  return 1
}

process_whine_mp4_target() {
  local target_mp4="$1"
  local ts="$2"
  local target_dir base extract_wav out_wav out_mp4

  target_dir="$(cd "$(dirname "$target_mp4")" && pwd)"
  base="$(file_stem "$target_mp4")"
  extract_wav="$target_dir/${base}_audio_original_${ts}.wav"
  out_wav="$target_dir/${base}_audio_whine_reduced_${ts}.wav"
  out_mp4="$target_dir/${base}_whine_reduced_${ts}.mp4"

  echo
  echo "Processing MP4: $target_mp4"
  echo "Extracting audio from MP4 ..."
  if ! extract_mp4_audio_wav "$target_mp4" "$extract_wav"; then
    echo "Audio extraction failed: $target_mp4"
    return 1
  fi

  if "$PY_BIN" "$PY_SCRIPT" whine \
    --target-wav "$extract_wav" \
    --out-wav "$out_wav" \
    "${WHINE_ARGS[@]}"; then
    echo "Writing processed audio back to MP4 ..."
    if remux_target_with_wav "$target_mp4" "$out_wav" "$out_mp4"; then
      rm -f "$extract_wav"
      echo "Output MP4: $out_mp4"
      echo "Output WAV: $out_wav"
      return 0
    fi
    echo "MP4 remux failed: $target_mp4"
    return 1
  fi

  echo "Whine-only processing failed: $target_mp4"
  return 1
}

process_audio_fix_wav_target() {
  local target_wav="$1"
  local ts="$2"
  local target_dir base out_wav
  local -a cmd

  target_dir="$(cd "$(dirname "$target_wav")" && pwd)"
  base="$(file_stem "$target_wav")"
  out_wav="$target_dir/${base}_fixed_${ts}.wav"

  cmd=(
    "$PY_BIN" "$PY_SCRIPT" fix
    --target-wav "$target_wav"
    --out-wav "$out_wav"
  )
  if (( ${#DYNAMICS_ARGS[@]} > 0 )); then
    cmd+=("${DYNAMICS_ARGS[@]}")
  fi
  if (( ${#DEESS_ARGS[@]} > 0 )); then
    cmd+=("${DEESS_ARGS[@]}")
  fi
  if (( ${#WHINE_ARGS[@]} > 0 )); then
    cmd+=("${WHINE_ARGS[@]}")
  fi

  echo
  echo "Fixing WAV: $target_wav"
  if "${cmd[@]}"; then
    echo "Output WAV: $out_wav"
    return 0
  fi

  echo "Audio fix failed: $target_wav"
  return 1
}

process_audio_fix_mp4_target() {
  local target_mp4="$1"
  local ts="$2"
  local target_dir base extract_wav out_wav out_mp4
  local -a cmd

  target_dir="$(cd "$(dirname "$target_mp4")" && pwd)"
  base="$(file_stem "$target_mp4")"
  extract_wav="$target_dir/${base}_audio_original_${ts}.wav"
  out_wav="$target_dir/${base}_audio_fixed_${ts}.wav"
  out_mp4="$target_dir/${base}_fixed_${ts}.mp4"
  cmd=(
    "$PY_BIN" "$PY_SCRIPT" fix
    --target-wav "$extract_wav"
    --out-wav "$out_wav"
  )
  if (( ${#DYNAMICS_ARGS[@]} > 0 )); then
    cmd+=("${DYNAMICS_ARGS[@]}")
  fi
  if (( ${#DEESS_ARGS[@]} > 0 )); then
    cmd+=("${DEESS_ARGS[@]}")
  fi
  if (( ${#WHINE_ARGS[@]} > 0 )); then
    cmd+=("${WHINE_ARGS[@]}")
  fi

  echo
  echo "Fixing MP4: $target_mp4"
  echo "Extracting audio from MP4 ..."
  if ! extract_mp4_audio_wav "$target_mp4" "$extract_wav"; then
    echo "Audio extraction failed: $target_mp4"
    return 1
  fi

  if "${cmd[@]}"; then
    echo "Writing fixed audio back to MP4 ..."
    if remux_target_with_wav "$target_mp4" "$out_wav" "$out_mp4"; then
      rm -f "$extract_wav"
      echo "Output MP4: $out_mp4"
      echo "Output WAV: $out_wav"
      return 0
    fi
    echo "MP4 remux failed: $target_mp4"
    return 1
  fi

  echo "Audio fix failed: $target_mp4"
  return 1
}

process_mp4_to_wav_target() {
  local target_mp4="$1"
  local out_wav

  out_wav="${target_mp4%.*}.wav"
  echo
  echo "Converting MP4: $target_mp4"
  if extract_mp4_audio_wav "$target_mp4" "$out_wav"; then
    echo "Saved WAV: $out_wav"
    return 0
  fi

  echo "MP4 -> WAV conversion failed: $target_mp4"
  return 1
}

write_pipeline_settings_file() {
  local settings_file="$1"
  local source_path="$2"
  local output_path="$3"
  local output_wav="$4"
  local ts="$5"
  shift 5
  local -a cmd=("$@")
  local arg

  {
    echo "Spectrum Transfer Settings"
    echo "=========================="
    echo "Timestamp: $ts"
    echo "Date: $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo
    echo "Source: $source_path"
    echo "Output: $output_path"
    if [[ -n "$output_wav" && "$output_wav" != "$output_path" ]]; then
      echo "Output WAV: $output_wav"
    fi
    echo
    echo "Selected settings:"
    if (( ${#DYNAMICS_ARGS[@]} > 0 )); then
      echo "- Auto level: on (${DYNAMICS_ARGS[*]})"
    else
      echo "- Auto level: off"
    fi
    if [[ -n "$SPECTRUM_REF" ]]; then
      echo "- Spectrum Master: on"
      echo "  Reference: $SPECTRUM_REF"
      if [[ -n "$SPECTRUM_REF_WAV" && "$SPECTRUM_REF_WAV" != "$SPECTRUM_REF" ]]; then
        echo "  Reference WAV: $SPECTRUM_REF_WAV"
      fi
    else
      echo "- Spectrum Master: off"
    fi
    if (( ${#DEESS_ARGS[@]} > 0 )); then
      echo "- De-Esser: on (${DEESS_ARGS[*]})"
    else
      echo "- De-Esser: off"
    fi
    if (( ${#CLARITY_ARGS[@]} > 0 )); then
      echo "- Voice Clarity: on (${CLARITY_ARGS[*]})"
    else
      echo "- Voice Clarity: off"
    fi
    if (( ${#SECOND_EQ_ARGS[@]} > 0 )); then
      echo "- Second Step EQ: on"
      echo "  160 Hz: -2.6 dB"
      echo "  200 Hz: -3.2 dB"
      echo "  8 kHz: +3.2 dB"
      echo "  10 kHz: +6.0 dB"
      echo "  12 kHz: +3.0 dB"
      echo "  16 kHz: -5.0 dB"
      echo "  20 kHz: -12.0 dB"
    else
      echo "- Second Step EQ: off"
    fi
    if (( ${#PEAK_NORMALIZE_ARGS[@]} > 0 )); then
      echo "- Peak normalizer: on (${PEAK_NORMALIZE_ARGS[*]})"
    else
      echo "- Peak normalizer: off"
    fi
    if (( ${#PEAK_CEILING_ARGS[@]} > 0 )); then
      echo "- Peak ceiling: on (${PEAK_CEILING_ARGS[*]})"
    else
      echo "- Peak ceiling: off"
    fi
    if (( ${#WHINE_ARGS[@]} > 0 )); then
      echo "- Whine reduction: on (${WHINE_ARGS[*]})"
    else
      echo "- Whine reduction: off"
    fi
    echo
    echo "Command:"
    printf "  "
    printf "%q " "${cmd[@]}"
    echo
  } > "$settings_file"
}

prepare_spectrum_reference() {
  local work_dir="$1"
  local ts="$2"
  local ext base

  SPECTRUM_REF_WAV=""

  if [[ -z "$SPECTRUM_REF" ]]; then
    return 0
  fi

  ext="$(lower_ext "$SPECTRUM_REF")"
  case "$ext" in
    wav)
      SPECTRUM_REF_WAV="$SPECTRUM_REF"
      return 0
      ;;
    csv)
      return 0
      ;;
    mp4)
      if ! ensure_ffmpeg; then
        return 1
      fi
      base="$(file_stem "$SPECTRUM_REF")"
      SPECTRUM_REF_WAV="$work_dir/${base}_desired_audio_${ts}.wav"
      echo "Extracting Spectrum Master reference MP4 audio once ..."
      extract_mp4_audio_wav "$SPECTRUM_REF" "$SPECTRUM_REF_WAV"
      return $?
      ;;
  esac

  return 1
}

process_pipeline_wav_target() {
  local target_wav="$1"
  local ts="$2"
  local target_dir base out_wav curve_csv preset settings_file spectrum_ext
  local -a cmd

  target_dir="$(cd "$(dirname "$target_wav")" && pwd)"
  base="$(file_stem "$target_wav")"
  out_wav="$target_dir/${base}_processed_${ts}.wav"
  curve_csv="$target_dir/${base}_curve_${ts}.csv"
  preset="$target_dir/${base}_audacity_${ts}.txt"
  settings_file="$target_dir/${base}_settings_${ts}.txt"

  cmd=(
    "$PY_BIN" "$PY_SCRIPT" pipeline
    --target-wav "$target_wav"
    --out-wav "$out_wav"
  )
  if (( ${#DYNAMICS_ARGS[@]} > 0 )); then
    cmd+=("${DYNAMICS_ARGS[@]}")
  fi
  if (( ${#DEESS_ARGS[@]} > 0 )); then
    cmd+=("${DEESS_ARGS[@]}")
  fi
  if (( ${#CLARITY_ARGS[@]} > 0 )); then
    cmd+=("${CLARITY_ARGS[@]}")
  fi
  if (( ${#SECOND_EQ_ARGS[@]} > 0 )); then
    cmd+=("${SECOND_EQ_ARGS[@]}")
  fi
  if (( ${#WHINE_ARGS[@]} > 0 )); then
    cmd+=("${WHINE_ARGS[@]}")
  fi
  if [[ -n "$SPECTRUM_REF" ]]; then
    spectrum_ext="$(lower_ext "$SPECTRUM_REF")"
    if [[ "$spectrum_ext" == "csv" ]]; then
      cmd+=(--spectrum-curve-csv "$SPECTRUM_REF")
    elif [[ -n "$SPECTRUM_REF_WAV" ]]; then
      cmd+=(
        --spectrum-reference-wav "$SPECTRUM_REF_WAV"
        --out-curve-csv "$curve_csv"
        --audacity-preset "$preset"
      )
    fi
  fi
  if (( ${#PEAK_NORMALIZE_ARGS[@]} > 0 )); then
    cmd+=("${PEAK_NORMALIZE_ARGS[@]}")
  fi
  if (( ${#PEAK_CEILING_ARGS[@]} > 0 )); then
    cmd+=("${PEAK_CEILING_ARGS[@]}")
  fi

  echo
  echo "Processing WAV: $target_wav"
  if "${cmd[@]}"; then
    write_pipeline_settings_file "$settings_file" "$target_wav" "$out_wav" "$out_wav" "$ts" "${cmd[@]}"
    echo "Output WAV: $out_wav"
    echo "Settings TXT: $settings_file"
    if [[ -n "$SPECTRUM_REF" && "$(lower_ext "$SPECTRUM_REF")" != "csv" ]]; then
      echo "Curve CSV:  $curve_csv"
      echo "Preset TXT: $preset"
    fi
    return 0
  fi

  echo "Processing failed: $target_wav"
  return 1
}

process_pipeline_mp4_target() {
  local target_mp4="$1"
  local ts="$2"
  local target_dir base extract_wav out_wav out_mp4 curve_csv preset settings_file spectrum_ext
  local -a cmd

  target_dir="$(cd "$(dirname "$target_mp4")" && pwd)"
  base="$(file_stem "$target_mp4")"
  extract_wav="$target_dir/${base}_audio_original_${ts}.wav"
  out_wav="$target_dir/${base}_audio_processed_${ts}.wav"
  out_mp4="$target_dir/${base}_processed_${ts}.mp4"
  curve_csv="$target_dir/${base}_curve_${ts}.csv"
  preset="$target_dir/${base}_audacity_${ts}.txt"
  settings_file="$target_dir/${base}_settings_${ts}.txt"

  echo
  echo "Processing MP4: $target_mp4"
  echo "Extracting audio from MP4 ..."
  if ! extract_mp4_audio_wav "$target_mp4" "$extract_wav"; then
    echo "Audio extraction failed: $target_mp4"
    return 1
  fi

  cmd=(
    "$PY_BIN" "$PY_SCRIPT" pipeline
    --target-wav "$extract_wav"
    --out-wav "$out_wav"
  )
  if (( ${#DYNAMICS_ARGS[@]} > 0 )); then
    cmd+=("${DYNAMICS_ARGS[@]}")
  fi
  if (( ${#DEESS_ARGS[@]} > 0 )); then
    cmd+=("${DEESS_ARGS[@]}")
  fi
  if (( ${#CLARITY_ARGS[@]} > 0 )); then
    cmd+=("${CLARITY_ARGS[@]}")
  fi
  if (( ${#SECOND_EQ_ARGS[@]} > 0 )); then
    cmd+=("${SECOND_EQ_ARGS[@]}")
  fi
  if (( ${#WHINE_ARGS[@]} > 0 )); then
    cmd+=("${WHINE_ARGS[@]}")
  fi
  if [[ -n "$SPECTRUM_REF" ]]; then
    spectrum_ext="$(lower_ext "$SPECTRUM_REF")"
    if [[ "$spectrum_ext" == "csv" ]]; then
      cmd+=(--spectrum-curve-csv "$SPECTRUM_REF")
    elif [[ -n "$SPECTRUM_REF_WAV" ]]; then
      cmd+=(
        --spectrum-reference-wav "$SPECTRUM_REF_WAV"
        --out-curve-csv "$curve_csv"
        --audacity-preset "$preset"
      )
    fi
  fi
  if (( ${#PEAK_NORMALIZE_ARGS[@]} > 0 )); then
    cmd+=("${PEAK_NORMALIZE_ARGS[@]}")
  fi
  if (( ${#PEAK_CEILING_ARGS[@]} > 0 )); then
    cmd+=("${PEAK_CEILING_ARGS[@]}")
  fi

  if "${cmd[@]}"; then
    echo "Writing processed audio back to MP4 ..."
    if remux_target_with_wav "$target_mp4" "$out_wav" "$out_mp4"; then
      write_pipeline_settings_file "$settings_file" "$target_mp4" "$out_mp4" "$out_wav" "$ts" "${cmd[@]}"
      rm -f "$extract_wav"
      echo "Output MP4: $out_mp4"
      echo "Output WAV: $out_wav"
      echo "Settings TXT: $settings_file"
      if [[ -n "$SPECTRUM_REF" && "$(lower_ext "$SPECTRUM_REF")" != "csv" ]]; then
        echo "Curve CSV:  $curve_csv"
        echo "Preset TXT: $preset"
      fi
      return 0
    fi
    echo "MP4 remux failed: $target_mp4"
    return 1
  fi

  echo "Processing failed: $target_mp4"
  return 1
}

run_spectrum_master_folder() {
  local a="$1"
  local folder="$2"
  local ea="$3"
  local ts="$4"
  local processed=0
  local failed=0
  local skipped=0
  local target ext desired_base desired_wav retry_processed retry_failed
  local -a failed_files retry_failed_files

  echo
  echo "Batch folder: $folder"
  echo "Generated files from previous runs are skipped."

  if [[ "$ea" == "wav" ]]; then
    if folder_contains_ext "$folder" "mp4" && ! ensure_ffmpeg; then
      return
    fi

    ask_dynamics_profile
    ask_deesser_profile
    ask_whine_profile

    failed_files=()
    while IFS= read -r target; do
      ext="$(lower_ext "$target")"
      if same_file_path "$a" "$target" || is_generated_artifact "$target"; then
        skipped=$((skipped + 1))
        continue
      fi

      if [[ "$ext" == "wav" ]]; then
        if process_match_wav_target "$a" "$target" "$ts"; then
          processed=$((processed + 1))
        else
          failed=$((failed + 1))
          failed_files+=("$target")
        fi
      elif [[ "$ext" == "mp4" ]]; then
        if process_desired_wav_target_mp4 "$a" "$target" "$ts"; then
          processed=$((processed + 1))
        else
          failed=$((failed + 1))
          failed_files+=("$target")
        fi
      fi
    done < <(find "$folder" -maxdepth 1 -type f \( -iname "*.wav" -o -iname "*.mp4" \) -print)

    echo
    echo "Batch done. Processed: $processed, failed: $failed, skipped: $skipped"
    if (( failed > 0 )); then
      print_failed_files "${failed_files[@]}"
      if ask_retry_failed_files; then
        retry_processed=0
        retry_failed=0
        retry_failed_files=()
        for target in "${failed_files[@]}"; do
          ext="$(lower_ext "$target")"
          if [[ "$ext" == "wav" ]]; then
            if process_match_wav_target "$a" "$target" "$ts"; then
              retry_processed=$((retry_processed + 1))
            else
              retry_failed=$((retry_failed + 1))
              retry_failed_files+=("$target")
            fi
          elif [[ "$ext" == "mp4" ]]; then
            if process_desired_wav_target_mp4 "$a" "$target" "$ts"; then
              retry_processed=$((retry_processed + 1))
            else
              retry_failed=$((retry_failed + 1))
              retry_failed_files+=("$target")
            fi
          fi
        done
        echo
        echo "Retry done. Processed: $retry_processed, failed: $retry_failed"
        print_failed_files "${retry_failed_files[@]}"
      fi
    fi
    wait_for_enter
    return
  fi

  if [[ "$ea" == "mp4" ]]; then
    if ! ensure_ffmpeg; then
      return
    fi

    desired_base="$(file_stem "$a")"
    desired_wav="$folder/${desired_base}_desired_audio_${ts}.wav"

    echo "Extracting desired MP4 audio once ..."
    if ! extract_mp4_audio_wav "$a" "$desired_wav"; then
      echo "Desired MP4 audio extraction failed."
      wait_for_enter
      return
    fi

    ask_dynamics_profile
    ask_deesser_profile
    ask_whine_profile

    failed_files=()
    while IFS= read -r target; do
      if same_file_path "$a" "$target" || is_generated_artifact "$target"; then
        skipped=$((skipped + 1))
        continue
      fi

      if process_desired_wav_target_mp4 "$desired_wav" "$target" "$ts"; then
        processed=$((processed + 1))
      else
        failed=$((failed + 1))
        failed_files+=("$target")
      fi
    done < <(find "$folder" -maxdepth 1 -type f -iname "*.mp4" -print)

    echo
    echo "Batch done. Processed: $processed, failed: $failed, skipped: $skipped"
    if (( failed > 0 )); then
      print_failed_files "${failed_files[@]}"
      if ask_retry_failed_files; then
        retry_processed=0
        retry_failed=0
        retry_failed_files=()
        for target in "${failed_files[@]}"; do
          if process_desired_wav_target_mp4 "$desired_wav" "$target" "$ts"; then
            retry_processed=$((retry_processed + 1))
          else
            retry_failed=$((retry_failed + 1))
            retry_failed_files+=("$target")
          fi
        done
        echo
        echo "Retry done. Processed: $retry_processed, failed: $retry_failed"
        print_failed_files "${retry_failed_files[@]}"
      fi
    fi
    wait_for_enter
    return
  fi

  if [[ "$ea" == "csv" ]]; then
    ask_dynamics_profile
    ask_deesser_profile
    ask_whine_profile

    failed_files=()
    while IFS= read -r target; do
      if is_generated_artifact "$target"; then
        skipped=$((skipped + 1))
        continue
      fi

      if process_apply_curve_target "$a" "$target" "$ts"; then
        processed=$((processed + 1))
      else
        failed=$((failed + 1))
        failed_files+=("$target")
      fi
    done < <(find "$folder" -maxdepth 1 -type f -iname "*.wav" -print)

    echo
    echo "Batch done. Processed: $processed, failed: $failed, skipped: $skipped"
    if (( failed > 0 )); then
      print_failed_files "${failed_files[@]}"
      if ask_retry_failed_files; then
        retry_processed=0
        retry_failed=0
        retry_failed_files=()
        for target in "${failed_files[@]}"; do
          if process_apply_curve_target "$a" "$target" "$ts"; then
            retry_processed=$((retry_processed + 1))
          else
            retry_failed=$((retry_failed + 1))
            retry_failed_files+=("$target")
          fi
        done
        echo
        echo "Retry done. Processed: $retry_processed, failed: $retry_failed"
        print_failed_files "${retry_failed_files[@]}"
      fi
    fi
    wait_for_enter
    return
  fi

  if [[ "$ea" == "txt" ]]; then
    ask_whine_profile

    failed_files=()
    while IFS= read -r target; do
      if same_file_path "$a" "$target" || is_generated_artifact "$target"; then
        skipped=$((skipped + 1))
        continue
      fi

      if process_curve_txt_target "$a" "$target" "$ts"; then
        processed=$((processed + 1))
      else
        failed=$((failed + 1))
        failed_files+=("$target")
      fi
    done < <(find "$folder" -maxdepth 1 -type f -iname "*.txt" -print)

    echo
    echo "Batch done. Processed: $processed, failed: $failed, skipped: $skipped"
    if (( failed > 0 )); then
      print_failed_files "${failed_files[@]}"
      if ask_retry_failed_files; then
        retry_processed=0
        retry_failed=0
        retry_failed_files=()
        for target in "${failed_files[@]}"; do
          if process_curve_txt_target "$a" "$target" "$ts"; then
            retry_processed=$((retry_processed + 1))
          else
            retry_failed=$((retry_failed + 1))
            retry_failed_files+=("$target")
          fi
        done
        echo
        echo "Retry done. Processed: $retry_processed, failed: $retry_failed"
        print_failed_files "${retry_failed_files[@]}"
      fi
    fi
    wait_for_enter
    return
  fi

  echo "Folder mode needs File 1 to be .wav, .mp4, .csv, or .txt."
  wait_for_enter
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
  echo "  File 2: target file or folder (wav/txt/mp4/folder)"
  echo
  echo "Supported combos: wav+wav, txt+txt, csv+wav, wav+mp4, mp4+mp4"
  echo "Folder mode: wav/mp4/csv/txt reference + target folder"

  a="$(ask_file_path 'File 1: ')"
  b="$(ask_file_path 'File 2: ')"

  if [[ -z "$a" || -z "$b" ]]; then
    echo "Both paths are required."
    wait_for_enter
    return
  fi

  if [[ ! -f "$a" ]]; then
    echo "File 1 must be an existing file."
    wait_for_enter
    return
  fi

  if [[ ! -f "$b" && ! -d "$b" ]]; then
    echo "File 2 must be an existing file or folder."
    wait_for_enter
    return
  fi

  if ! ensure_python_version; then
    return
  fi
  ensure_python_env
  ea="$(lower_ext "$a")"
  ts="$(stamp)"

  if [[ -d "$b" ]]; then
    run_spectrum_master_folder "$a" "$(cd "$b" && pwd)" "$ea" "$ts"
    return
  fi

  eb="$(lower_ext "$b")"

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
    ask_whine_profile

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

    ask_whine_profile

    if "$PY_BIN" "$PY_SCRIPT" curve \
      --desired-spectrum "$a" \
      --target-spectrum "$b" \
      --curve-csv "$out_csv" \
      --audacity-preset "$out_preset" \
      "${WHINE_ARGS[@]}"; then
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
    ask_whine_profile

    if "$PY_BIN" "$PY_SCRIPT" apply \
      --curve-csv "$curve" \
      --target-wav "$target" \
      --out-wav "$out_wav" \
      "${DYNAMICS_ARGS[@]}" \
      "${DEESS_ARGS[@]}" \
      "${WHINE_ARGS[@]}"; then
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
    ask_whine_profile

    if run_match_command "$desired_wav" "$extract_wav" "$matched_wav" "$out_csv" "$out_preset"; then
      echo "Writing processed audio back to MP4 ..."
      remux_target_with_wav "$target_mp4" "$matched_wav" "$out_mp4"
      rm -f "$extract_wav"
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
    ask_whine_profile

    if run_match_command "$desired_wav" "$extract_wav" "$matched_wav" "$out_csv" "$out_preset"; then
      echo "Writing processed audio back to target MP4 ..."
      remux_target_with_wav "$target_mp4" "$matched_wav" "$out_mp4"
      rm -f "$extract_wav"
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
  local in_mp4 target processed failed skipped retry_processed retry_failed
  local -a failed_files retry_failed_files

  if ! ensure_ffmpeg; then
    return
  fi

  echo
  in_mp4="$(ask_file_path 'MP4 file or folder: ')"
  if [[ -z "$in_mp4" || ( ! -f "$in_mp4" && ! -d "$in_mp4" ) ]]; then
    echo "Valid MP4 path or folder required."
    wait_for_enter
    return
  fi

  if [[ -d "$in_mp4" ]]; then
    processed=0
    failed=0
    skipped=0
    failed_files=()
    echo
    echo "Batch folder: $in_mp4"
    echo "Generated files from previous runs are skipped."

    while IFS= read -r target; do
      if is_generated_artifact "$target"; then
        skipped=$((skipped + 1))
        continue
      fi

      if process_mp4_to_wav_target "$target"; then
        processed=$((processed + 1))
      else
        failed=$((failed + 1))
        failed_files+=("$target")
      fi
    done < <(find "$in_mp4" -maxdepth 1 -type f -iname "*.mp4" -print)

    echo
    echo "Batch done. Processed: $processed, failed: $failed, skipped: $skipped"
    if (( failed > 0 )); then
      print_failed_files "${failed_files[@]}"
      if ask_retry_failed_files; then
        retry_processed=0
        retry_failed=0
        retry_failed_files=()
        for target in "${failed_files[@]}"; do
          if process_mp4_to_wav_target "$target"; then
            retry_processed=$((retry_processed + 1))
          else
            retry_failed=$((retry_failed + 1))
            retry_failed_files+=("$target")
          fi
        done
        echo
        echo "Retry done. Processed: $retry_processed, failed: $retry_failed"
        print_failed_files "${retry_failed_files[@]}"
      fi
    fi
    wait_for_enter
    return
  fi

  if [[ "$(lower_ext "$in_mp4")" != "mp4" ]]; then
    echo "Input must be an .mp4 file."
    wait_for_enter
    return
  fi

  process_mp4_to_wav_target "$in_mp4"
  wait_for_enter
}

run_whine_only() {
  local in_file ext ts target_dir base out_wav out_mp4 extract_wav
  local target processed failed skipped retry_processed retry_failed
  local -a failed_files retry_failed_files

  echo
  in_file="$(ask_file_path 'WAV/MP4 file or folder: ')"
  if [[ -z "$in_file" || ( ! -f "$in_file" && ! -d "$in_file" ) ]]; then
    echo "Valid WAV/MP4 path or folder required."
    wait_for_enter
    return
  fi

  if ! ensure_python_version; then
    return
  fi
  ensure_python_env

  ts="$(stamp)"

  ask_whine_profile

  if [[ ${#WHINE_ARGS[@]} -eq 0 ]]; then
    echo "Whine reduction is off, nothing to do."
    wait_for_enter
    return
  fi

  if [[ -d "$in_file" ]]; then
    if folder_contains_ext "$in_file" "mp4" && ! ensure_ffmpeg; then
      return
    fi

    processed=0
    failed=0
    skipped=0
    failed_files=()
    echo
    echo "Batch folder: $in_file"
    echo "Generated files from previous runs are skipped."

    while IFS= read -r target; do
      ext="$(lower_ext "$target")"
      if is_generated_artifact "$target"; then
        skipped=$((skipped + 1))
        continue
      fi

      if [[ "$ext" == "wav" ]]; then
        if process_whine_wav_target "$target" "$ts"; then
          processed=$((processed + 1))
        else
          failed=$((failed + 1))
          failed_files+=("$target")
        fi
      elif [[ "$ext" == "mp4" ]]; then
        if process_whine_mp4_target "$target" "$ts"; then
          processed=$((processed + 1))
        else
          failed=$((failed + 1))
          failed_files+=("$target")
        fi
      fi
    done < <(find "$in_file" -maxdepth 1 -type f \( -iname "*.wav" -o -iname "*.mp4" \) -print)

    echo
    echo "Batch done. Processed: $processed, failed: $failed, skipped: $skipped"
    if (( failed > 0 )); then
      print_failed_files "${failed_files[@]}"
      if ask_retry_failed_files; then
        retry_processed=0
        retry_failed=0
        retry_failed_files=()
        for target in "${failed_files[@]}"; do
          ext="$(lower_ext "$target")"
          if [[ "$ext" == "wav" ]]; then
            if process_whine_wav_target "$target" "$ts"; then
              retry_processed=$((retry_processed + 1))
            else
              retry_failed=$((retry_failed + 1))
              retry_failed_files+=("$target")
            fi
          elif [[ "$ext" == "mp4" ]]; then
            if process_whine_mp4_target "$target" "$ts"; then
              retry_processed=$((retry_processed + 1))
            else
              retry_failed=$((retry_failed + 1))
              retry_failed_files+=("$target")
            fi
          fi
        done
        echo
        echo "Retry done. Processed: $retry_processed, failed: $retry_failed"
        print_failed_files "${retry_failed_files[@]}"
      fi
    fi
    wait_for_enter
    return
  fi

  ext="$(lower_ext "$in_file")"
  target_dir="$(cd "$(dirname "$in_file")" && pwd)"

  if [[ "$ext" == "wav" ]]; then
    base="$(file_stem "$in_file")"
    out_wav="$target_dir/${base}_whine_reduced_${ts}.wav"

    if "$PY_BIN" "$PY_SCRIPT" whine \
      --target-wav "$in_file" \
      --out-wav "$out_wav" \
      "${WHINE_ARGS[@]}"; then
      echo "Output WAV: $out_wav"
    else
      echo "Whine-only processing failed."
    fi
    wait_for_enter
    return
  fi

  if [[ "$ext" == "mp4" ]]; then
    if ! ensure_ffmpeg; then
      return
    fi

    base="$(file_stem "$in_file")"
    extract_wav="$target_dir/${base}_audio_original_${ts}.wav"
    out_wav="$target_dir/${base}_audio_whine_reduced_${ts}.wav"
    out_mp4="$target_dir/${base}_whine_reduced_${ts}.mp4"

    echo "Extracting audio from MP4 ..."
    extract_mp4_audio_wav "$in_file" "$extract_wav"

    if "$PY_BIN" "$PY_SCRIPT" whine \
      --target-wav "$extract_wav" \
      --out-wav "$out_wav" \
      "${WHINE_ARGS[@]}"; then
      echo "Writing processed audio back to MP4 ..."
      remux_target_with_wav "$in_file" "$out_wav" "$out_mp4"
      rm -f "$extract_wav"
      echo "Output MP4: $out_mp4"
      echo "Output WAV: $out_wav"
    else
      echo "Whine-only processing failed."
    fi
    wait_for_enter
    return
  fi

  echo "Only .wav and .mp4 are supported here."
  wait_for_enter
}

run_audio_fix() {
  local in_file ext ts target processed failed skipped retry_processed retry_failed
  local -a failed_files retry_failed_files

  echo
  in_file="$(ask_file_path 'WAV/MP4 file or folder: ')"
  if [[ -z "$in_file" || ( ! -f "$in_file" && ! -d "$in_file" ) ]]; then
    echo "Valid WAV/MP4 path or folder required."
    wait_for_enter
    return
  fi

  if ! ensure_python_version; then
    return
  fi
  ensure_python_env

  ts="$(stamp)"

  ask_dynamics_profile
  ask_deesser_profile
  ask_whine_profile

  if [[ ${#DYNAMICS_ARGS[@]} -eq 0 && ${#DEESS_ARGS[@]} -eq 0 && ${#WHINE_ARGS[@]} -eq 0 ]]; then
    echo "Auto-Level, De-Esser, and Whine reduction are off, nothing to do."
    wait_for_enter
    return
  fi

  if [[ -d "$in_file" ]]; then
    if folder_contains_ext "$in_file" "mp4" && ! ensure_ffmpeg; then
      return
    fi

    processed=0
    failed=0
    skipped=0
    failed_files=()
    echo
    echo "Batch folder: $in_file"
    echo "Generated files from previous runs are skipped."

    while IFS= read -r target; do
      ext="$(lower_ext "$target")"
      if is_generated_artifact "$target"; then
        skipped=$((skipped + 1))
        continue
      fi

      if [[ "$ext" == "wav" ]]; then
        if process_audio_fix_wav_target "$target" "$ts"; then
          processed=$((processed + 1))
        else
          failed=$((failed + 1))
          failed_files+=("$target")
        fi
      elif [[ "$ext" == "mp4" ]]; then
        if process_audio_fix_mp4_target "$target" "$ts"; then
          processed=$((processed + 1))
        else
          failed=$((failed + 1))
          failed_files+=("$target")
        fi
      fi
    done < <(find "$in_file" -maxdepth 1 -type f \( -iname "*.wav" -o -iname "*.mp4" \) -print)

    echo
    echo "Batch done. Processed: $processed, failed: $failed, skipped: $skipped"
    if (( failed > 0 )); then
      print_failed_files "${failed_files[@]}"
      if ask_retry_failed_files; then
        retry_processed=0
        retry_failed=0
        retry_failed_files=()
        for target in "${failed_files[@]}"; do
          ext="$(lower_ext "$target")"
          if [[ "$ext" == "wav" ]]; then
            if process_audio_fix_wav_target "$target" "$ts"; then
              retry_processed=$((retry_processed + 1))
            else
              retry_failed=$((retry_failed + 1))
              retry_failed_files+=("$target")
            fi
          elif [[ "$ext" == "mp4" ]]; then
            if process_audio_fix_mp4_target "$target" "$ts"; then
              retry_processed=$((retry_processed + 1))
            else
              retry_failed=$((retry_failed + 1))
              retry_failed_files+=("$target")
            fi
          fi
        done
        echo
        echo "Retry done. Processed: $retry_processed, failed: $retry_failed"
        print_failed_files "${retry_failed_files[@]}"
      fi
    fi
    wait_for_enter
    return
  fi

  ext="$(lower_ext "$in_file")"

  if [[ "$ext" == "wav" ]]; then
    process_audio_fix_wav_target "$in_file" "$ts"
    wait_for_enter
    return
  fi

  if [[ "$ext" == "mp4" ]]; then
    if ! ensure_ffmpeg; then
      return
    fi

    process_audio_fix_mp4_target "$in_file" "$ts"
    wait_for_enter
    return
  fi

  echo "Only .wav and .mp4 are supported here."
  wait_for_enter
}

run_audio_pipeline() {
  local mode="${1:-manual}"
  local in_path ext ts target_dir target processed failed skipped retry_processed retry_failed
  local -a failed_files retry_failed_files

  echo
  in_path="$(ask_file_path 'WAV/MP4 file or folder: ')"
  if [[ -z "$in_path" || ( ! -f "$in_path" && ! -d "$in_path" ) ]]; then
    echo "Valid WAV/MP4 path or folder required."
    wait_for_enter
    return
  fi

  if ! ensure_python_version; then
    return
  fi
  ensure_python_env

  ts="$(stamp)"

  if [[ "$mode" == "fast" ]]; then
    set_fast_pipeline_defaults
  else
    echo
    echo "Choose optional processing steps in this order:"
    echo "  1) Auto level"
    echo "  2) Spectrum Master"
    echo "  3) De-Esser"
    echo "  4) Voice Clarity"
    echo "  5) Second Step EQ"
    echo "  6) Peak normalizer"
    echo "  7) Peak ceiling"
    echo "  8) Whine"

    ask_dynamics_profile
    ask_spectrum_profile
    ask_deesser_profile
    ask_voice_clarity_profile
    ask_second_eq_profile
    ask_peak_normalizer_profile
    ask_peak_ceiling_profile
    ask_whine_profile
  fi

  if [[ ${#DYNAMICS_ARGS[@]} -eq 0 \
    && ${#DEESS_ARGS[@]} -eq 0 \
    && ${#CLARITY_ARGS[@]} -eq 0 \
    && ${#SECOND_EQ_ARGS[@]} -eq 0 \
    && ${#WHINE_ARGS[@]} -eq 0 \
    && -z "$SPECTRUM_REF" \
    && ${#PEAK_NORMALIZE_ARGS[@]} -eq 0 \
    && ${#PEAK_CEILING_ARGS[@]} -eq 0 ]]; then
    echo "No processing steps selected, nothing to do."
    wait_for_enter
    return
  fi

  if [[ -d "$in_path" ]]; then
    if folder_contains_ext "$in_path" "mp4" && ! ensure_ffmpeg; then
      return
    fi
    if ! prepare_spectrum_reference "$(cd "$in_path" && pwd)" "$ts"; then
      echo "Spectrum reference preparation failed."
      wait_for_enter
      return
    fi

    processed=0
    failed=0
    skipped=0
    failed_files=()
    echo
    echo "Batch folder: $in_path"
    echo "Generated files from previous runs are skipped."

    while IFS= read -r target; do
      ext="$(lower_ext "$target")"
      if is_generated_artifact "$target"; then
        skipped=$((skipped + 1))
        continue
      fi

      if [[ "$ext" == "wav" ]]; then
        if process_pipeline_wav_target "$target" "$ts"; then
          processed=$((processed + 1))
        else
          failed=$((failed + 1))
          failed_files+=("$target")
        fi
      elif [[ "$ext" == "mp4" ]]; then
        if process_pipeline_mp4_target "$target" "$ts"; then
          processed=$((processed + 1))
        else
          failed=$((failed + 1))
          failed_files+=("$target")
        fi
      fi
    done < <(find "$in_path" -maxdepth 1 -type f \( -iname "*.wav" -o -iname "*.mp4" \) -print)

    echo
    echo "Batch done. Processed: $processed, failed: $failed, skipped: $skipped"
    if (( failed > 0 )); then
      print_failed_files "${failed_files[@]}"
      if ask_retry_failed_files; then
        retry_processed=0
        retry_failed=0
        retry_failed_files=()
        for target in "${failed_files[@]}"; do
          ext="$(lower_ext "$target")"
          if [[ "$ext" == "wav" ]]; then
            if process_pipeline_wav_target "$target" "$ts"; then
              retry_processed=$((retry_processed + 1))
            else
              retry_failed=$((retry_failed + 1))
              retry_failed_files+=("$target")
            fi
          elif [[ "$ext" == "mp4" ]]; then
            if process_pipeline_mp4_target "$target" "$ts"; then
              retry_processed=$((retry_processed + 1))
            else
              retry_failed=$((retry_failed + 1))
              retry_failed_files+=("$target")
            fi
          fi
        done
        echo
        echo "Retry done. Processed: $retry_processed, failed: $retry_failed"
        print_failed_files "${retry_failed_files[@]}"
      fi
    fi
    wait_for_enter
    return
  fi

  ext="$(lower_ext "$in_path")"
  target_dir="$(cd "$(dirname "$in_path")" && pwd)"

  if ! prepare_spectrum_reference "$target_dir" "$ts"; then
    echo "Spectrum reference preparation failed."
    wait_for_enter
    return
  fi

  if [[ "$ext" == "wav" ]]; then
    process_pipeline_wav_target "$in_path" "$ts"
    wait_for_enter
    return
  fi

  if [[ "$ext" == "mp4" ]]; then
    if ! ensure_ffmpeg; then
      return
    fi

    process_pipeline_mp4_target "$in_path" "$ts"
    wait_for_enter
    return
  fi

  echo "Only .wav and .mp4 are supported here."
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
  echo "1) Audio Pipeline (file/folder)"
  echo "2) Fast Pipeline (standard settings, file/folder)"
  echo "3) MP4 -> WAV (file/folder)"
  echo "4) Exit"
  echo
  printf "Choose [1/2/3/4]: "
  IFS= read -r choice

  case "${choice:-}" in
    1)
      run_audio_pipeline
      ;;
    2)
      run_audio_pipeline fast
      ;;
    3)
      run_mp4_to_wav
      ;;
    4)
      echo "Bye."
      exit 0
      ;;
    *)
      echo "Invalid choice."
      wait_for_enter
      ;;
  esac
done
