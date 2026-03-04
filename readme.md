# PZMod Sync Tool

<!-- ALL-CONTRIBUTORS-BADGE:START - Do not remove or modify this section -->
[![All Contributors](https://img.shields.io/badge/all_contributors-0-orange.svg?style=flat-square)](#contributors)
<!-- ALL-CONTRIBUTORS-BADGE:END -->
[![CI](https://github.com/CyiceK/PZMod_synchronization/actions/workflows/ci.yml/badge.svg)](https://github.com/CyiceK/PZMod_synchronization/actions/workflows/ci.yml)
[![Release](https://github.com/CyiceK/PZMod_synchronization/actions/workflows/release.yml/badge.svg)](https://github.com/CyiceK/PZMod_synchronization/actions/workflows/release.yml)

[中文文档](./readme.zh-CN.md)

## Project Overview

PZMod Sync Tool is a desktop toolkit for Project Zomboid mod synchronization and save/map diagnostics.
It helps keep client mod lists, dedicated server configuration, and world-save analysis workflows aligned.

GitHub: https://github.com/CyiceK/PZMod_synchronization

## Features

- MOD discovery, filtering, sorting, and batch enable/disable
- Server MOD config synchronization workflow
- Save and map analysis utilities
- Runtime log viewer and debug diagnostics controls
- Multi-language UI (English, Simplified Chinese, Traditional Chinese)

## Screens / Navigation Overview

Main navigation includes:

- Home
- MOD Manager
- Server Sync
- Save Manager
- Map Manager
- Symlink
- Logs
- Log Analysis
- About
- Settings

The About page contains:

- Project overview
- Contributors template block
- Acknowledgements template block
- References template block
- `Open GitHub` action button

## Quick Start

### Requirements

- Python 3.10+
- Windows / macOS / Linux

### Install & Run

```bash
git clone https://github.com/CyiceK/PZMod_synchronization.git
cd PZMod_synchronization
pip install -r requirements.txt
python main.py
```

## Configuration

In `Settings`, configure these paths on first run:

- Workshop path (Steam workshop content directory)
- Game path (Project Zomboid install directory)
- Document/save path (`Zomboid` user data directory)

## Development & Testing

### Development

```bash
pip install -r requirements.txt
python main.py
```

### Testing

```bash
python -m compileall .
pytest -q
```

If needed, run a focused set of tests for specific modules first, then full regression.

## Contribution

Issues and pull requests are welcome.

Please include:

- Clear reproduction steps for bug reports
- Scope and rationale for behavior changes
- Tests for non-trivial logic changes

## Contributors

Template:

- Name / Handle: `<placeholder>`
- Contribution Area: `<placeholder>`

<!-- ALL-CONTRIBUTORS-LIST:START - Do not remove or modify this section -->
<!-- ALL-CONTRIBUTORS-LIST:END -->

## Acknowledgements

Template:

- Thanks to `<person or project>` for `<support details>`

## References

Template:

- Referenced project: `<xxx project>` (`<url>`)

## License

This project is licensed under [GPL-3.0](LICENSE).
