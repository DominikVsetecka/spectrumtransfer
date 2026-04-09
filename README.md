# spectrumtransfer

Transfer vocal tone from a reference recording to a target recording, or remove high-frequency electrical whine from WAV and MP4 audio.

Core idea:
`EQ delta = desired_spectrum_dB - target_spectrum_dB`

## What It Does

- Spectrum/EQ transfer (`match`) from desired WAV to target WAV
- Curve-only workflow from Audacity spectrum `.txt` files (`curve`)
- Apply saved curve to WAV (`apply`)
- Whine-only cleanup with upper-band cut plus optional notch filters (`whine`)
- Optional de-esser
- Optional auto-level + compressor + limiter
- MP4 audio extraction/remux workflows via the master command
- Single user-facing launcher: `spectrumtransfer.command`

## Platform Notes

- `spectrumtransfer.command` is a macOS launcher (Terminal `.command` file).
- `spectrumtransfer.py` is platform-independent Python and can run on macOS, Linux, and Windows (with dependencies installed).

## Requirements

- Python 3.9+
- `ffmpeg` in PATH (for MP4 workflows)
- Python packages: `numpy`, `scipy`

Install Python deps:

```bash
python3 -m pip install numpy scipy
```

## Main Entry Point (Recommended)

Use only this launcher:

```bash
./spectrumtransfer.command
```

The launcher validates `python3` and requires version `>= 3.9` before creating `.venv` or installing dependencies.

Menu:

1. `Spectrum Master (EQ + Auto-Level + optional De-Esser)`
2. `Whine Only (WAV/MP4)`
3. `MP4 -> WAV`
4. `Exit`

### Supported Spectrum Master Input Pairs

- `desired.wav` + `target.wav`
- `desired.txt` + `target.txt`
- `curve.csv` + `target.wav` (order independent)
- `desired.wav` + `target.mp4` (order independent)
- `desired.mp4` + `target.mp4` (desired first, target second)

## CLI (Advanced)

### 1) Match desired WAV to target WAV

```bash
python3 spectrumtransfer.py match \
  --desired-wav desired_reference.wav \
  --target-wav vocal_to_fix.wav \
  --out-wav vocal_matched.wav \
  --curve-csv eq_curve.csv \
  --audacity-preset audacity_filter_curve.txt
```

Useful flags:

- `--whine-high-cut` / `--whine-notch-hz`
- `--de-ess` (+ `--de-ess-*` params)
- `--auto-level` (+ dynamics params)

### 2) Build curve from Audacity spectrum exports

```bash
python3 spectrumtransfer.py curve \
  --desired-spectrum desired_spectrum.txt \
  --target-spectrum target_spectrum.txt \
  --curve-csv eq_curve.csv \
  --audacity-preset audacity_filter_curve.txt
```

### 3) Apply saved curve to WAV

```bash
python3 spectrumtransfer.py apply \
  --curve-csv eq_curve.csv \
  --target-wav vocal_to_fix.wav \
  --out-wav vocal_matched.wav
```

### 4) Remove only electrical whine from WAV

```bash
python3 spectrumtransfer.py whine \
  --target-wav vocal_to_fix.wav \
  --out-wav vocal_whine_reduced.wav \
  --whine-high-cut \
  --whine-high-cut-freq-hz 17400 \
  --whine-high-cut-gain-db -30 \
  --whine-high-cut-q 1.2 \
  --whine-notch-hz 15600 16800 18100
```

## Notes

- This is static spectrum matching (tonal balance), not adaptive dynamic EQ.
- For problematic WAV headers, the process may warn but still continue.

## Changelog

See [CHANGELOG.md](./CHANGELOG.md).
