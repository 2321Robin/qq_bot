from __future__ import annotations

import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qq_bot.services.roco_bwiki import parse_pet_detail  # noqa: E402


DIMO_URL = "https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB"
DEFAULT_OUTPUT_PATH = ROOT / "data" / "roco_pet_details" / "迪莫.json"


def fetch_html(url: str) -> str:
    request = Request(url, headers={"User-Agent": "qq-bot-roco-detail-fetcher/0.1"})
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def write_pet_detail(detail: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(detail, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    try:
        html = fetch_html(DIMO_URL)
    except (HTTPError, URLError, TimeoutError) as exc:
        print(f"Failed to fetch {DIMO_URL}: {exc}", file=sys.stderr)
        return 1

    detail = parse_pet_detail(DIMO_URL, html)
    write_pet_detail(detail, DEFAULT_OUTPUT_PATH)
    print(f"Wrote {DEFAULT_OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
