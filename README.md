# spectrumtransfer

Transfer a static EQ spectrum from one vocal recording to another.

This tool implements the same core idea discussed in the Audacity thread:  
`EQ delta = desired_spectrum_dB - target_spectrum_dB`.

It supports:
- WAV -> WAV matching (derive and apply in one step)
- Audacity spectrum `.txt` exports -> EQ curve
- Audacity Filter Curve EQ preset text export
- Quick MP4 -> WAV audio extraction
- macOS `.command` launchers with English prompts/messages

## Requirements

- Python 3.9+
- `numpy`
- `scipy`

Install:

```bash
python3 -m pip install numpy scipy
```

## 1) Match one vocal WAV to another

```bash
python3 eq_transfer.py match \
  --desired-wav desired_reference.wav \
  --target-wav vocal_to_fix.wav \
  --out-wav vocal_matched.wav \
  --curve-csv eq_curve.csv \
  --audacity-preset audacity_filter_curve.txt
```

Useful tuning options:
- `--smooth-octaves 0.20` default; larger is smoother.
- `--max-cut-db -12` and `--max-boost-db 12` clamp extreme EQ.
- `--mix 0.0..1.0` blend processed signal with dry signal.
- `--min-freq 60 --max-freq 16000` vocal-focused matching range.
- `--match-reverb` enables optional heuristic room matching.
- `--reverb-mode auto|add|remove` chooses strategy (auto can add or reduce).
- `--reverb-strength 1.0` controls room-processing intensity (0..2).

### Room/Reverb Parameter Guide

Room matching is optional and only applies to the `match` command.

- `--match-reverb`: enables room estimation + post-processing.
- `--reverb-mode auto`:
  - adds reverb if the target sounds drier than desired.
  - removes reverb if the target sounds wetter than desired.
- `--reverb-mode add`: only adds reverb, never dereverbs.
- `--reverb-mode remove`: only dereverbs, never adds reverb.
- `--reverb-strength` range is `0..2`:
  - `0.3..0.7`: subtle/natural adjustment
  - `0.8..1.2`: normal range (CLI default is `1.0`)
  - `>1.2`: aggressive processing; use carefully

Example:

```bash
python3 eq_transfer.py match \
  --desired-wav desired_reference.wav \
  --target-wav vocal_to_fix.wav \
  --out-wav vocal_matched.wav \
  --curve-csv eq_curve.csv \
  --audacity-preset audacity_filter_curve.txt \
  --match-reverb \
  --reverb-mode auto \
  --reverb-strength 0.7
```

## 2) Build curve from Audacity spectrum text exports

1. In Audacity, run `Analyze -> Plot Spectrum -> Export...` for both clips:
- desired reference clip
- target clip

2. Build delta curve:

```bash
python3 eq_transfer.py curve \
  --desired-spectrum desired_spectrum.txt \
  --target-spectrum target_spectrum.txt \
  --curve-csv eq_curve.csv \
  --audacity-preset audacity_filter_curve.txt
```

Then either:
- load `audacity_filter_curve.txt` values into Audacity Filter Curve EQ preset flow, or
- apply the saved `eq_curve.csv` to a WAV with command (3).

## 3) Apply a saved curve to a WAV

```bash
python3 eq_transfer.py apply \
  --curve-csv eq_curve.csv \
  --target-wav vocal_to_fix.wav \
  --out-wav vocal_matched.wav
```

## Notes

- This is a **static** spectrum match (long-term tonal balance), not dynamic EQ.
- Input audio support is WAV (PCM/float).
- Heavy curve boosts can increase noise/sibilance; reduce with `--max-boost-db` and more smoothing.

## Extra: MP4 -> WAV

Requires `ffmpeg` in your PATH.

Basic:

```bash
python3 mp4_to_wav.py input.mp4
```

Custom output/sample rate/channels:

```bash
python3 mp4_to_wav.py input.mp4 \
  --output-wav extracted.wav \
  --sample-rate 44100 \
  --channels 1
```

## macOS Drag&Drop (.command)

You can use these launcher files directly in Finder:

- `mp4_to_wav.command`
- `eq_transfer.command`

### `mp4_to_wav.command`

Open it (double-click), then drag one `.mp4` path into the terminal window and press Enter.  
The `.wav` is created next to the source file.

### `eq_transfer.command`

Open it (double-click), then enter 2 file paths in terminal (drag&drop from Finder), then press Enter.
On first run it creates a local `.venv` and installs required Python packages automatically.
For MP4 workflows, `ffmpeg` must be installed and available in PATH.
For all match modes, it asks for room mode:
- `0`: off
- `1`: auto (recommended, add or remove)
- `2`: add
- `3`: remove (dereverb)

Launcher presets map to these CLI values:
- `0` -> no reverb flags (disabled)
- `1` -> `--match-reverb --reverb-mode auto --reverb-strength 0.55`
- `2` -> `--match-reverb --reverb-mode add --reverb-strength 0.45`
- `3` -> `--match-reverb --reverb-mode remove --reverb-strength 0.75`

All user-facing launcher prompts and status/error messages are in English.

Supported combinations:

1. `desired.wav` + `target.wav`  
Creates matched WAV + curve CSV + Audacity preset TXT.

2. `desired.txt` + `target.txt`  
Creates curve CSV + Audacity preset TXT.

3. `curve.csv` + `target.wav` (order does not matter)  
Applies curve and creates processed WAV.

4. `desired.wav` + `target.mp4` (order does not matter)  
Extracts MP4 audio to WAV, matches EQ, and writes a new matched MP4.

5. `desired.mp4` + `target.mp4` (order matters: desired first, target second)  
Extracts both MP4 audios to WAV, matches target audio to desired audio, and writes a new matched target MP4.

## Changelog

See [CHANGELOG.md](./CHANGELOG.md) for release history and updates.
