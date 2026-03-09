# spectrumtransfer

Transfer vocal tone from a reference recording to a target recording with optional de-essing and loudness control.

Core idea:
`EQ delta = desired_spectrum_dB - target_spectrum_dB`

## What It Does

- Spectrum/EQ transfer (`match`) from desired WAV to target WAV
- Curve-only workflow from Audacity spectrum `.txt` files (`curve`)
- Apply saved curve to WAV (`apply`)
- Optional de-esser
- Optional auto-level + compressor + limiter
- MP4 audio extraction/remux workflows via the master command
- Single user-facing launcher: `spectrum_master.command`

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
./spectrum_master.command
```

Menu:

1. `Spectrum Master (EQ + Auto-Level + optional De-Esser)`
2. `MP4 -> WAV`
3. `Exit`

### Supported Spectrum Master Input Pairs

- `desired.wav` + `target.wav`
- `desired.txt` + `target.txt`
- `curve.csv` + `target.wav` (order independent)
- `desired.wav` + `target.mp4` (order independent)
- `desired.mp4` + `target.mp4` (desired first, target second)

## CLI (Advanced)

### 1) Match desired WAV to target WAV

```bash
python3 eq_transfer.py match \
  --desired-wav desired_reference.wav \
  --target-wav vocal_to_fix.wav \
  --out-wav vocal_matched.wav \
  --curve-csv eq_curve.csv \
  --audacity-preset audacity_filter_curve.txt
```

Useful flags:

- `--de-ess` (+ `--de-ess-*` params)
- `--auto-level` (+ dynamics params)

### 2) Build curve from Audacity spectrum exports

```bash
python3 eq_transfer.py curve \
  --desired-spectrum desired_spectrum.txt \
  --target-spectrum target_spectrum.txt \
  --curve-csv eq_curve.csv \
  --audacity-preset audacity_filter_curve.txt
```

### 3) Apply saved curve to WAV

```bash
python3 eq_transfer.py apply \
  --curve-csv eq_curve.csv \
  --target-wav vocal_to_fix.wav \
  --out-wav vocal_matched.wav
```

## Notes

- This is static spectrum matching (tonal balance), not adaptive dynamic EQ.
- For problematic WAV headers, the process may warn but still continue.

## Changelog

See [CHANGELOG.md](./CHANGELOG.md).
