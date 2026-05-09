# Roco Pet Database Design

## Goal

Add a small local Rock Kingdom World pet database to the QQ bot, focused on pet lookup and evolution conditions.

## Scope

Version 1 uses local JSON data and explicit commands only. It does not scrape pages at runtime and does not inject pet data into normal AI chat yet.

## Data

Create `data/roco_pets.json` with 5-10 starter records. Each record contains:

- `name`: primary pet name.
- `aliases`: alternate names for lookup.
- `number`: pet number when available.
- `attributes`: one or more attributes.
- `stage`: current form or stage.
- `evolution_chain`: ordered names in the evolution chain.
- `evolution_condition`: concise evolution condition text.
- `source_url`: page used to verify the data.

Missing fields should be displayed as `未知`, not invented.

## Commands

Add a plugin `src/qq_bot/plugins/roco.py` with these commands:

- `/精灵 <名称>`
- `/洛克 <名称>`

Both commands should respect the existing group allowlist. If the query is empty, reply with usage. If no local record matches, reply that the local database has not collected that pet yet.

## Service

Add `src/qq_bot/services/roco_pets.py` to:

- load local JSON data;
- model records as small dataclasses;
- find pets by exact name, alias, or substring match;
- format one matched record for QQ group text.

## Verification

- Unit tests cover loading, name/alias lookup, missing lookup, and formatted evolution condition output.
- Plugin helper tests cover usage text and not-found text.
- Full test suite, Ruff, and bot import should pass.
