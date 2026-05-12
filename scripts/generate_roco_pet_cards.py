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

DETAIL_DIR = ROOT / "data" / "roco_pet_details"
ASSET_DIR = ROOT / "data" / "roco_assets"
CARD_DIR = ROOT / "data" / "roco_pet_cards"


def main() -> None:
    records = load_pet_records_from_details(DETAIL_DIR)
    attribute_icon_stats = ensure_attribute_icon_assets(records, asset_directory=ASSET_DIR)
    asset_stats = ensure_pet_art_assets(records, asset_directory=ASSET_DIR)
    generated_paths = generate_pet_card_files(records, output_directory=CARD_DIR, asset_directory=ASSET_DIR)
    generated_count = len(generated_paths)
    print(
        f"Loaded {len(records)} pet record(s); "
        f"attribute icons {attribute_icon_stats}; assets {asset_stats}; generated {generated_count} card(s)"
    )


if __name__ == "__main__":
    main()
