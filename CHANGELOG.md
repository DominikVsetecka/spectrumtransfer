# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and this project uses Semantic Versioning.

## [Unreleased]

### Changed
- Translated all user-facing launcher text in `eq_transfer.command` and `mp4_to_wav.command` to English.
- Updated `README.md` to reflect English launcher prompts and added a changelog reference.
- Expanded documentation for room/reverb parameters (`--match-reverb`, `--reverb-mode`, `--reverb-strength`) with examples and launcher-to-CLI mapping.

## [0.1.0] - 2026-02-25

### Added
- Initial `eq_transfer.py` tool for static EQ transfer (`match`, `curve`, `apply`).
- `mp4_to_wav.py` utility for MP4 audio extraction.
- macOS `.command` launchers for drag-and-drop terminal workflows.
- Basic project documentation and dependency setup.
