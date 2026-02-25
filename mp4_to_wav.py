#!/usr/bin/env python3
"""
Extract audio from an MP4 file and save as WAV.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Extract WAV audio from an MP4 file via ffmpeg.")
    p.add_argument("input_mp4", type=Path, help="Input .mp4 file")
    p.add_argument(
        "-o",
        "--output-wav",
        type=Path,
        help="Output .wav file (default: same name as input, .wav extension)",
    )
    p.add_argument("--sample-rate", type=int, default=48000, help="Output sample rate (default: 48000)")
    p.add_argument("--channels", type=int, default=1, help="Output channel count (default: 1)")
    return p


def main() -> int:
    args = build_parser().parse_args()
    input_mp4 = args.input_mp4

    if not input_mp4.exists():
        raise SystemExit(f"Input file not found: {input_mp4}")
    if input_mp4.suffix.lower() != ".mp4":
        raise SystemExit(f"Expected an .mp4 file, got: {input_mp4}")

    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not found in PATH. Install ffmpeg first.")

    output_wav = args.output_wav or input_mp4.with_suffix(".wav")
    output_wav.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_mp4),
        "-vn",
        "-map",
        "0:a:0",
        "-acodec",
        "pcm_s16le",
        "-ar",
        str(args.sample_rate),
        "-ac",
        str(args.channels),
        str(output_wav),
    ]

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"ffmpeg failed with exit code {exc.returncode}") from exc

    print(f"Saved WAV: {output_wav}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
