# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and this project uses Semantic Versioning.

## [Unreleased]

### Added
- Electrical whine reduction with configurable upper-band cut and up to three narrow notch frequencies.
- Dedicated `whine` processing mode for applying only whine removal to a WAV without spectrum matching or post-processing.
- Launcher support for `Whine Only (WAV/MP4)` including MP4 extract/process/remux workflow.

### Changed
- Extended `match`, `curve`, and `apply` flows so whine reduction can be layered onto existing EQ processing.

## [0.1.1] - 2026-03-09

### Changed
- Unified launcher workflow into a single macOS entrypoint: `spectrumtransfer.command`.
- Renamed core script from `eq_transfer.py` to `spectrumtransfer.py` and updated launcher/README references.
- Added explicit Python version gate in launcher (`python3 >= 3.9`) before environment setup.
- Added dynamic loudness chain controls (auto-level, compressor, limiter) and de-esser options to processing flow.
- Removed room/reverb feature from the user-facing workflow and documentation.
- Updated `README.md` with platform notes: `.command` launcher is macOS-specific, Python script is cross-platform.

## [0.1.0] - 2026-02-25

### Added
- Initial `eq_transfer.py` tool for static EQ transfer (`match`, `curve`, `apply`).
- `mp4_to_wav.py` utility for MP4 audio extraction.
- macOS `.command` launchers for drag-and-drop terminal workflows.
- Basic project documentation and dependency setup.
