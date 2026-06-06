# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows semantic versioning while it remains useful for bot releases.

## [Unreleased]

### Added

- Added maintainable runtime version lookup backed by the project version.
- Added `/version` and `/版本` commands for checking the running bot version.
- Added help text and README references for version and changelog support.
- Added AI chat grounding on local 洛克王国精灵 and skill data for natural-language evolution and multi-skill questions.
- Added structured evolution normalization so source, middle, and target forms all record forward and backward evolution text.
- Added BWiki raw-template fetching support for newly published pets.
- Added S2 season local evolution data for pets 348, 354, 356, 358, 360, 362, 365, 367, 369, 371, 373, and 375.
- Added image-sourced special evolution conditions for bloodline, skill-use, typed-defeat, time, weather, sex, height, random-form, mining, friend-world, and starlight evolutions.

### Changed

- Included the computed weekday in AI current-time grounding to avoid mismatched date and weekday replies.
- Refreshed local 洛克王国精灵 details through 图鉴编号 375.

## [0.1.0] - 2026-06-04

### Added

- Initial QQ group bot baseline with NoneBot2 and OneBot v11 integration.
- Added basic help command support.
- Added local 洛克王国精灵 and skill lookup commands.
- Added AI chat, group memory, search-enhanced replies, scheduled group messages, and capture counting features.
