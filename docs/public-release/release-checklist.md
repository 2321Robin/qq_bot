# Public Release Checklist

> Stage 0 — Repository public-readiness gate.
> Each line records a verification action, its date, tool version,
> exit code or conclusion, and the reviewer's initials.
>
> No secrets, full personal paths, QQ account numbers, or raw scan
> output are recorded here.

## Token revocation (S0-SEC-01, S0-SEC-05)

- [x] Old NapCat WebUI token revoked in NapCat admin interface
  - Date: 2026-07-29  |  Reviewer: Robin
  - Method: Replaced token value in `webui.json`; old token no longer valid.
- [ ] New final token generated after history clean-up
  - Current token in effect; re-generate after history cleanup if performed.

## Current-tree scan (S0-SEC-02, S0-GATE-03)

- [ ] Gitleaks dir scan passes with zero un-ignored hits
  - Version: **8.30.1**
  - Date: \_\_\_\_\_\_\_\_  |  Exit code: \_\_\_\_\_\_\_\_  |  Un-ignored hits: \_\_\_\_\_\_\_\_
- [ ] All hits resolved or explicitly allowed in `.gitleaks.toml`

## Full-history scan (S0-SEC-04, S0-GATE-03)

- [ ] Gitleaks git scan passes with zero un-ignored hits
  - Date: \_\_\_\_\_\_\_\_  |  Exit code: \_\_\_\_\_\_\_\_  |  Un-ignored hits: \_\_\_\_\_\_\_\_
- [ ] All hits resolved: zero un-ignored hits in intended-public history

## License and copyright (S0-LICENSE-01, S0-LICENSE-02)

- [x] Copyright holder confirmed: Robin
- [x] License type: MIT
- [x] Year: 2026
- [x] `LICENSE` file present and accurate

## Data and image attribution (S0-LICENSE-03, S0-LICENSE-04)

- [x] `DATA_LICENSE.md` present
- [x] Public tree excludes data without redistribution right
- [x] Decision: Only code and test fixtures in public tree

## Privacy (S0-PRIVACY-01 through S0-PRIVACY-05)

- [x] `PRIVACY.md` present
- [x] Content reviewed against current code

## Screenshot redaction (S0-DOC-04, S0-DOC-05, S0-GATE-04)

- [ ] Screenshot redaction checklist filled and verified
  - **Status:** Deferred — not required for initial release
  - Template `docs/public-release/screenshot-redaction-checklist.md` is ready.

## Quality gates (S0-GATE-03)

- [ ] `pytest -q` — exit code: **0**
- [ ] `ruff check .` — exit code: **0**
- [ ] Final `git status --short` clean of blocked files

## Final gate

- [ ] All S0-CONFIG, S0-SEC, S0-LICENSE, S0-PRIVACY, S0-DOC, S0-GATE
      "must" requirements satisfied
- [ ] Repository ready for public visibility
