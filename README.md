# spectrumtransfer

Transfer vocal tone from a reference recording to a target recording, or remove high-frequency electrical whine from WAV and MP4 audio.

This tool is useful when recordings are technically usable, but do not sound consistent enough for delivery. A typical case is a studio or office video production where one take was recorded with a cleaner mic chain, better placement, or a more controlled room, while another take sounds thinner, duller, harsher, or slightly off because of a different camera mic, lav setup, gain staging, or speaker position.

Instead of rebuilding the sound by hand every time, `spectrumtransfer` can derive a static tonal correction from a good reference and apply it to the weaker recording. That makes it practical for interview shoots, talking-head videos, podcast video sessions, training content, social snippets, and other post-production workflows where multiple recordings should feel like they came from the same setup.

It also helps in the common case where the voice is basically fine, but the recording contains a narrow high-frequency electrical whine from studio gear, screens, power supplies, lighting, or camera electronics. In that situation you can use the dedicated whine-only mode to clean the noise without changing the rest of the signal more than necessary.

Core idea:
`EQ delta = desired_spectrum_dB - target_spectrum_dB`

## What It Does

- Spectrum/EQ transfer (`match`) from desired WAV to target WAV
- Curve-only workflow from Audacity spectrum `.txt` files (`curve`)
- Apply saved curve to WAV (`apply`)
- Audio fix workflow (`fix`) for whine reduction, auto-level, and optional de-esser without EQ transfer
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

1. `Audio Pipeline (file/folder)`
2. `Fast Pipeline (standard settings, file/folder)`
3. `Exit`

The pipeline asks for a target `.wav`/`.mp4` file or a folder first, then offers these optional steps in order:

- Auto level
- Spectrum Master (`.wav`, `.csv`, or `.mp4` audio/curve reference; `.txt` spectrum references remain curve-only)
- De-Esser
- Peak normalizer to `-6 dBFS`
- Peak ceiling at `-6 dBFS`
- Whine reduction

Folder processing is non-recursive and skips generated output files from previous runs.

If a folder batch has failed files, the launcher prints the affected file paths at the end and asks whether only those failed files should be retried.

Batch MP4 processing runs `ffmpeg` with stdin disabled so ffmpeg cannot consume the internal file list while a folder is being processed.

Fast Pipeline uses these defaults without asking for every processing choice: auto-level gentle, de-esser gentle, Spectrum Master off, peak normalizer `-6 dBFS`, peak ceiling `-6 dBFS`, and whine reduction gentle.

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

### Pipeline mode

```bash
python3 spectrumtransfer.py pipeline \
  --target-wav vocal_to_fix.wav \
  --out-wav vocal_processed.wav \
  --auto-level \
  --de-ess \
  --whine-high-cut \
  --spectrum-reference-wav desired_reference.wav \
  --peak-normalize \
  --peak-ceiling
```

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

### 5) Apply whine reduction, auto-level, and de-esser

```bash
python3 spectrumtransfer.py fix \
  --target-wav vocal_to_fix.wav \
  --out-wav vocal_fixed.wav \
  --whine-high-cut \
  --whine-notch-hz 15600 16800 18100 \
  --auto-level \
  --de-ess
```

## Notes

- This is static spectrum matching (tonal balance), not adaptive dynamic EQ.
- For problematic WAV headers, the process may warn but still continue.

## Changelog

See [CHANGELOG.md](./CHANGELOG.md).
