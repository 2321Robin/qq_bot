# Roco Pet Card Design

## Goal

Upgrade Rock Kingdom World pet lookup from plain text to a local static PNG card similar to the supplied dark rounded pet card mockup.

## Scope

Version 1 only generates static local cards for records already present in `data/roco_pets.json`. It does not fetch pet avatars from the internet, scrape live pages, or add a full skill/acquisition/evolution-branch system.

If a card field is missing, the renderer should show `未知`, an empty stat bar, or omit non-critical decoration. Lookup should still work.

## Data Model

Extend `PetRecord` and JSON records with optional card fields:

- `height_weight`: compact display text such as `5.5~7KG`.
- `body_length`: compact display text such as `0.54~0.78M`.
- `favorite_partner`: short label for the orange section title.
- `description`: one-line card description.
- `race_value`: total race value shown in the stats header.
- `stats`: object with numeric values for `hp`, `physical_attack`, `magic_attack`, `physical_defense`, `magic_defense`, and `speed`.

Existing fields keep their current meaning and continue supporting text fallback formatting.

## Rendering

Add a focused service module, `src/qq_bot/services/roco_pet_cards.py`, responsible only for turning a `PetRecord` into PNG bytes.

The renderer should use Pillow and draw:

- dark background with two rounded panels;
- circular placeholder avatar using the pet name or attribute color, because real avatars are out of scope;
- top metadata row with number and attributes;
- height/weight and body-length pills;
- orange favorite-partner label and description text;
- race value header;
- six stat rows with icons or text labels, orange progress bars, and right-aligned values;
- footer text noting local/generated data source.

Chinese text must use a local system font when available. If the preferred font is unavailable, rendering should fall back to Pillow's default font without crashing.

## Bot Behavior

`/精灵 <名称>`, `/洛克 <名称>`, and @机器人 pet lookup should find records exactly as they do today.

When a record is found, the plugin should try to generate and send a PNG image via OneBot `MessageSegment.image`. If image generation fails, the bot should send the existing text representation instead, so pet lookup remains usable.

Usage and not-found messages stay as text.

## Dependencies

Add Pillow as a runtime dependency in `pyproject.toml`.

## Testing

Add or update tests to cover:

- loading records that include the new optional card fields;
- card generation returns PNG bytes for a sample record;
- missing optional fields do not break card generation;
- `/精灵` and mention lookup send an image-like message for found pets;
- plugin fallback sends text if card generation raises an exception;
- existing text formatting and lookup tests continue passing.

## Manual Verification

After implementation, start the bot and send `/精灵 迪莫` in an allowed group. The expected result is one PNG card image. Send an unknown pet name and expect the existing not-found text.
