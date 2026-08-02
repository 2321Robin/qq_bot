"""Incrementally regenerate pet cards for the records affected by a data refresh.

Without ``--change-set`` the script keeps its legacy behavior: every record is
regenerated. With ``--change-set data/manifests/change_set.json`` only records
listed as added/modified plus records whose evolution chain or evolution edges
reference a changed name are redrawn (S3-INCR-05).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qq_bot.services.roco_pet_cards import (  # noqa: E402
    ensure_attribute_icon_assets,
    ensure_pet_art_assets,
    generate_pet_card_files,
    load_pet_records_from_details,
)
from qq_bot.services.roco_pets import PetRecord  # noqa: E402

DETAIL_DIR = ROOT / "data" / "roco_pet_details"
ASSET_DIR = ROOT / "data" / "roco_assets"
CARD_DIR = ROOT / "data" / "roco_pet_cards"


def _changed_name(entry: str) -> str:
    """Map a change-set entry (file name like ``440-睡铃雪影娃娃.json`` or a bare
    pet name) to the pet name it refers to."""
    stem = Path(entry).stem
    if "-" in stem:
        return stem.split("-", 1)[1]
    return stem


def affected_records(records: list[PetRecord], change_set: dict) -> list[PetRecord]:
    """changed = added + modified；受影响集 = changed ∪ 进化链/进化边引用 changed 名的记录。"""
    changed_names = {
        _changed_name(entry)
        for entry in change_set.get("added", []) + change_set.get("modified", [])
    }
    affected = [record for record in records if record.name in changed_names]
    for record in records:
        if record.name in changed_names:
            continue
        chain = set(record.evolution_chain or [record.name])
        references_changed = bool(chain & changed_names) or any(
            relation.source in changed_names or relation.target in changed_names
            for relation in record.evolution_to
        )
        if references_changed:
            affected.append(record)
    return affected


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--change-set",
        type=Path,
        default=None,
        help="data/manifests/change_set.json；只重绘受影响记录，缺省全量",
    )
    parser.add_argument("--details-dir", type=Path, default=DETAIL_DIR)
    parser.add_argument("--cards-dir", type=Path, default=CARD_DIR)
    parser.add_argument("--assets-dir", type=Path, default=ASSET_DIR)
    # main() without arguments (legacy call from tests) means full generation.
    args = parser.parse_args([]) if argv is None else parser.parse_args(argv)

    records = load_pet_records_from_details(args.details_dir)
    if args.change_set is None:
        attribute_icon_stats = ensure_attribute_icon_assets(
            records, asset_directory=args.assets_dir
        )
        asset_stats = ensure_pet_art_assets(records, asset_directory=args.assets_dir)
        generated_paths = generate_pet_card_files(
            records, output_directory=args.cards_dir, asset_directory=args.assets_dir
        )
        print(
            f"Loaded {len(records)} pet record(s); "
            f"attribute icons {attribute_icon_stats}; assets {asset_stats}; "
            f"generated {len(generated_paths)} card(s)"
        )
        return

    change_set = json.loads(args.change_set.read_text(encoding="utf-8"))
    affected = affected_records(records, change_set)
    if affected:
        asset_stats = ensure_pet_art_assets(affected, asset_directory=args.assets_dir)
        generated_paths = generate_pet_card_files(
            affected, output_directory=args.cards_dir, asset_directory=args.assets_dir
        )
    else:
        asset_stats = {"existing": 0, "fetched": 0, "failed": 0}
        generated_paths = []
    print(
        f"Loaded {len(records)} pet record(s); affected={len(affected)} "
        f"total={len(records)}; assets {asset_stats}; generated {len(generated_paths)} card(s)"
    )


if __name__ == "__main__":
    main(sys.argv[1:])
