from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    from qq_bot.services.roco_pet_cards import generate_pet_card_files
    from qq_bot.services.roco_pets import get_pet_records

    paths = generate_pet_card_files(get_pet_records())
    print(f"Generated {len(paths)} pet card(s) in data/roco_pet_cards")


if __name__ == "__main__":
    main()
