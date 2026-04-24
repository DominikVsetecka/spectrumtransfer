# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and this project uses Semantic Versioning.

## [Unreleased]

### Added
- Unified launcher `Audio Pipeline` workflow: target file/folder first, then optional auto-level, spectrum matching, de-esser, peak normalize, peak ceiling, and whine.
- Fast Pipeline launcher option with standard settings for auto-level gentle, de-esser gentle, peak normalize/ceiling at `-6 dBFS`, and whine gentle.
- `pipeline` CLI mode for applying the same ordered processing chain to WAV files.
- `fix` CLI mode for applying whine reduction, auto-level, and optional de-esser without EQ transfer.
- Folder batch summaries now list failed files and offer a retry pass for only those files.

### Fixed
- Prevented `ffmpeg` from consuming the batch file list from stdin, which could truncate subsequent paths and cause false "No such file or directory" errors.

### Removed
- Direct launcher menu entry for `MP4 -> WAV`.

## [0.1.2] - 2026-04-09

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
