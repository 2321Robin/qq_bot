# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows semantic versioning while it remains useful for bot releases.

## [Unreleased]

### Added

- Added stage-3 data pipeline (offline-first): schema contracts with strict field validation and quarantine of invalid detail files (`data/quarantine/` with `{name}.error.json`), per-file hashes and `dataset_hash` manifests (`data/manifests/latest.json` → `previous.json` rotation), incremental BWiki refresh with 304/content-hash short-circuits, configurable quality gates (record floor, net drop, number gaps, stats/race completeness, dangling edges, skill-key rates, quarantine), machine-readable diff reports (`data/reports/refresh-*.{json,md}`), atomic publish via staging, `--change-set` incremental card regeneration, prebuilt n-gram search index (`data/roco_search.sqlite3`) with full-scan fallback, and `clear_record_caches()` hot-reload hooks.
- Added refresh orchestration command `scripts/refresh_roco_data.py` (console script `refresh-roco-data`) with `--offline/--dry-run/--verify-only/--force/--prune-removed/--allow-quarantine` and exit codes 0/1/2.
- Added private distribution commands: `package-roco-data` (details + `latest.json` + `sha256SUMS.txt` tarball keyed by `dataset_hash`) and `download-roco-data` (required `--base-url`/`--dataset-hash`, per-file sha256 verification, atomic install, hash-keyed cache under `data/.cache/`); no built-in public URL (data is not publicly redistributable — `DATA_LICENSE.md` §6).
- Added CI data-pipeline gate job (offline dry-run over fixtures, report structure assertion, manifest verify-only consistency).
- Added CI pipeline (GitHub Actions) with quality, test matrix (Python 3.11 / 3.12), security (Gitleaks) and container jobs.
- Added offline evaluation gate to CI: frozen dataset + manifest hash; tampered or missing manifest fails the pipeline (`scripts/run_agent_eval.py --mode validate|offline`).
- Added pre-commit hooks (ruff format/check, Gitleaks secret scan).
- Added Dockerfile and Compose deployment for a non-root backend image with named data volume.
- Added `/healthz` and `/readyz` health endpoints (liveness / readiness incl. SQLite migrations).
- Added asyncio SQLite chat-memory repository with versioned, transactional migrations.
- Added reliability layer: classified retries (AI / Tavily / QQ send) with capped exponential backoff and jitter, per-dependency circuit breakers, and explicit never-retry semantics for ambiguous QQ send timeouts.
- Added branch-coverage gate with a measured baseline (`fail_under = 82`).
- Added maintainable runtime version lookup backed by the project version.
- Added `/version` and `/版本` commands for checking the running bot version.
- Added help text and README references for version and changelog support.
- Added AI chat grounding on local 洛克王国精灵 and skill data for natural-language evolution and multi-skill questions.
- Added structured evolution normalization so source, middle, and target forms all record forward and backward evolution text.
- Added BWiki raw-template fetching support for newly published pets.
- Added S2 season local evolution data for pets 348, 354, 356, 358, 360, 362, 365, 367, 369, 371, 373, and 375.
- Added image-sourced special evolution conditions for bloodline, skill-use, typed-defeat, time, weather, sex, height, random-form, mining, friend-world, and starlight evolutions.
- Added stage-2 structured tool-calling agent (`AGENT_ENABLED`): keyword router over four routes (local knowledge / web search / chat memory / direct chat) with confidence thresholds and clarification replies; initial tool registry (pet lookup, skill intersection, evolution routes, web search, chat memory); orchestrator with round/call/deadline limits, per-source token budget and safe failure messages; evidence store with deterministic grounding checks (always on) and optional semantic verifier; grounded answer rendering with cited sources.
- Added layered chat memory (recent messages, opt-in short-term summaries that never extend retention, explicit long-term preferences via `/记忆保存`) with user-facing commands `/记忆保存` `/记忆查看` `/记忆删除` `/记忆关闭`, and a repository-backed service.
- Added agent runtime wiring: plugin switches to the agent path when `AGENT_ENABLED=true`, legacy stage-1 pipeline stays as the rollback path; runtime exposes the agent stack with explicit not-ready errors.
- Added stage-2 evaluation: 150-case frozen dataset (tool selection, facts, citations, refusals, fabrication) with dev/test splits, offline runner with metric gates and manifest hash, and a live benchmark (`AGENT_EVAL_LIVE=1` + provider config) comparing the legacy pipeline against the tool agent on the same split with desensitized reports.

### Changed

- Included the computed weekday in AI current-time grounding to avoid mismatched date and weekday replies.
- Refreshed local 洛克王国精灵 details through 图鉴编号 375.
- Chat memory reads/writes are now fully asynchronous; event handlers no longer block the event loop on SQLite.
- Scheduled-message sending and interactive sending share the same error classification and breaker; logs are redacted to counts and categories (no message bodies, raw ids, or credentials).
- Named mentions in scheduled messages and AI replies (`@昵称` → @at) now resolve through `NAMED_MENTION_REPLACEMENTS` configuration instead of hardcoded accounts, so deployers keep real QQ numbers out of the public repository.

### Removed

- Removed obsolete `/计数` capture counting command and local counter configuration.

## [0.1.0] - 2026-06-04

## [0.1.0] - 2026-06-04

### Added

- Initial QQ group bot baseline with NoneBot2 and OneBot v11 integration.
- Added basic help command support.
- Added local 洛克王国精灵 and skill lookup commands.
- Added AI chat, group memory, search-enhanced replies, scheduled group messages, and capture counting features.
