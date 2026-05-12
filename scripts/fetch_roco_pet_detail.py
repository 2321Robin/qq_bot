from __future__ import annotations

import json
from http.client import RemoteDisconnected
from pathlib import Path
import subprocess
import sys
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qq_bot.services.roco_bwiki import PARSER_VERSION, parse_pet_detail  # noqa: E402


DIMO_URL = "https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB"
BWIKI_INDEX_URL = "https://wiki.biligame.com/rocom/%E7%B2%BE%E7%81%B5%E7%AD%9B%E9%80%89"
PETS_PATH = ROOT / "data" / "roco_pets.json"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "roco_pet_details"
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def fetch_html(url: str) -> str:
    request = Request(url, headers=BROWSER_HEADERS)
    try:
        with urlopen(request, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError):
        return _fetch_html_with_curl(url)


def _fetch_html_with_curl(url: str) -> str:
    command = [
        "curl.exe",
        "-L",
        "--retry",
        "2",
        "--max-time",
        "30",
        "-A",
        BROWSER_HEADERS["User-Agent"],
        "-H",
        f"Accept: {BROWSER_HEADERS['Accept']}",
        "-H",
        f"Accept-Language: {BROWSER_HEADERS['Accept-Language']}",
        url,
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise URLError(result.stderr.strip() or f"curl.exe exited with {result.returncode}")
    return result.stdout


def write_pet_detail(detail: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(detail, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_fetch_targets(pets_path: Path = PETS_PATH) -> list[tuple[str, str]]:
    pets = json.loads(pets_path.read_text(encoding="utf-8"))
    targets: list[tuple[str, str]] = []
    for pet in pets:
        name = pet.get("name", "")
        source_url = pet.get("source_url", "")
        parsed = urlparse(source_url)
        if name and parsed.netloc == "wiki.biligame.com" and parsed.path.startswith("/rocom/"):
            targets.append((name, _quote_url(source_url)))
    return targets


class _BwikiIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, str]]] = []
        self._row: list[tuple[str, str]] | None = None
        self._cell_tag = ""
        self._cell_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell_tag = tag
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._row is not None and self._cell_tag == tag:
            text = " ".join("".join(self._cell_parts).split())
            self._row.append((tag, text))
            self._cell_tag = ""
            self._cell_parts = []
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell_tag:
            self._cell_parts.append(data)


def load_bwiki_index_targets(html: str) -> list[tuple[str, str]]:
    parser = _BwikiIndexParser()
    parser.feed(html)

    targets: list[tuple[str, str]] = []
    name_index: int | None = None
    for row in parser.rows:
        values = [cell_text for _, cell_text in row]
        if not values:
            continue
        if any(cell_tag == "th" for cell_tag, _ in row) and "精灵名称" in values:
            name_index = values.index("精灵名称")
            continue
        if name_index is None or name_index >= len(values):
            continue

        name = values[name_index]
        if name and name not in {"精灵名称", ""}:
            targets.append((name, _quote_url(f"https://wiki.biligame.com/rocom/{name}")))
    return _dedupe_targets(targets)


def _quote_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(
        parsed._replace(path=quote(parsed.path, safe="/%"), query=quote(parsed.query, safe="=&%"))
    )


def _dedupe_targets(targets: list[tuple[str, str]]) -> list[tuple[str, str]]:
    unique_targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, url in targets:
        if name in seen:
            continue
        seen.add(name)
        unique_targets.append((name, url))
    return unique_targets


def fetch_pet_details(
    targets: list[tuple[str, str]],
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    fetch_html_func=fetch_html,
    force: bool = False,
    min_parser_version: int = PARSER_VERSION,
    retries: int = 0,
    delay_seconds: float = 0,
) -> list[tuple[str, str, str]]:
    errors: list[tuple[str, str, str]] = []
    for name, url in targets:
        output_path = _existing_output_path(output_dir, name) or output_dir / f"{name}.json"
        if output_path.exists() and not force and _is_current_detail(output_path, min_parser_version):
            print(f"Skipped {output_path}")
            continue
        try:
            html = _fetch_with_retries(url, fetch_html_func, retries, delay_seconds)
        except (HTTPError, RemoteDisconnected, URLError, TimeoutError) as exc:
            errors.append((name, url, str(exc)))
            continue

        detail = parse_pet_detail(url, html)
        numbered_output_path = _numbered_output_path(output_dir, name, detail, output_path)
        write_pet_detail(detail, numbered_output_path)
        if output_path != numbered_output_path and output_path.exists():
            output_path.unlink()
        output_path = numbered_output_path
        print(f"Wrote {output_path}")
    return errors


def _fetch_with_retries(url: str, fetch_html_func, retries: int, delay_seconds: float) -> str:
    attempts = retries + 1
    for attempt in range(attempts):
        try:
            return fetch_html_func(url)
        except (HTTPError, RemoteDisconnected, URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
            if delay_seconds > 0:
                time.sleep(delay_seconds)
    raise RuntimeError("unreachable")


def _is_current_detail(output_path: Path, min_parser_version: int) -> bool:
    try:
        detail = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    parser_version = detail.get("metadata", {}).get("parser_version", 0)
    return isinstance(parser_version, int) and parser_version >= min_parser_version


def _existing_output_path(output_dir: Path, name: str) -> Path | None:
    numbered_matches = sorted(output_dir.glob(f"*-{name}.json"))
    if numbered_matches:
        return numbered_matches[0]
    name_path = output_dir / f"{name}.json"
    if name_path.exists():
        return name_path
    return None


def _numbered_output_path(output_dir: Path, fallback_name: str, detail: dict, existing_path: Path) -> Path:
    name = str(detail.get("name") or fallback_name)
    number = str(detail.get("profile", {}).get("编号", "")).strip()
    if number:
        return output_dir / f"{number}-{name}.json"
    if existing_path.name != f"{fallback_name}.json":
        return existing_path
    return output_dir / f"{name}.json"


def main() -> int:
    try:
        index_html = fetch_html(BWIKI_INDEX_URL)
    except (HTTPError, URLError, TimeoutError) as exc:
        print(f"Failed to fetch {BWIKI_INDEX_URL}: {exc}", file=sys.stderr)
        return 1

    targets = load_bwiki_index_targets(index_html)
    errors = fetch_pet_details(targets)
    for name, url, error in errors:
        print(f"Failed to fetch {name} ({url}): {error}", file=sys.stderr)
    if errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
