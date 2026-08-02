"""Incremental fetch tests with a fake fetcher (S3-INCR-01..04, all offline)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from qq_bot.datapipeline.fetch import (
    FetchResponse,
    classify,
    conditional_headers,
    incremental_fetch,
)
from qq_bot.datapipeline.manifest import (
    LicenseInfo,
    ManifestEntry,
    RefreshManifest,
    compute_dataset_hash,
)
import qq_bot.datapipeline.fetch as fetch_module

FIXED_TIME = datetime(2026, 2, 1, tzinfo=timezone.utc)


# ---- platform-specific curl binary (regression: Linux runners have plain curl) ----


def test_curl_fallback_uses_platform_binary(monkeypatch) -> None:
    import os
    import subprocess

    captured: list[list[str]] = []

    def fake_run(command, **kwargs):
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="<body>", stderr="")

    monkeypatch.setattr(fetch_module.subprocess, "run", fake_run)
    expected = "curl.exe" if os.name == "nt" else "curl"
    response = fetch_module.UrllibFetcher()._fetch_with_curl("https://example.com/x")
    assert response.body == "<body>"
    assert captured[0][0] == expected


def _raw_template(name: str, number: str = "101", extra: str = "") -> str:
    return f"""{{{{精灵信息
    |精灵名称={name}
    |主属性=光
    |生命=120
    |物攻=80
    |魔攻=80
    |物防=105
    |魔防=105
    |速度=92
    |进化条件=无法进化
    {extra}
    }}}}"""


def _body_sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _make_manifest(
    files: dict[str, str],
    *,
    etags: dict[str, str] | None = None,
    last_modified: dict[str, str] | None = None,
) -> RefreshManifest:
    entries: dict[str, ManifestEntry] = {}
    hashes: dict[str, str] = {}
    for name, body in files.items():
        hashes[name] = _body_sha(body)
        entries[name] = ManifestEntry(
            source_url=f"https://example.com/pets/{name}",
            sha256=hashes[name],
            size=len(body.encode("utf-8")),
            parser_version=6,
            fetched_at=FIXED_TIME,
            etag=(etags or {}).get(name),
            last_modified=(last_modified or {}).get(name),
        )
    return RefreshManifest(
        refreshed_at=FIXED_TIME,
        index_url="https://example.com/index",
        license=LicenseInfo(
            source="synthetic",
            claim="private",
            attribution_required=True,
            commercial_use=False,
            redistribution="private-only",
            game_assets="proprietary",
        ),
        entries=entries,
        dataset_hash=compute_dataset_hash(hashes),
    )


class FakeFetcher:
    def __init__(self, responses: dict[str, FetchResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResponse:
        self.calls.append((url, headers))
        return self.responses[url]


def _targets(names: list[tuple[str, str]]) -> list[tuple[str, str, dict[str, str]]]:
    return [(name, f"https://example.com/pets/{name}", {"精灵编号": num}) for name, num in names]


def test_classify_304_is_unchanged() -> None:
    assert classify("abc", FetchResponse(status=304, headers={}, body="")) == "unchanged"


def test_looks_like_challenge_page() -> None:
    challenge = (
        "<html><head><title></title></head><body><style>*{margin:0;padding:0;"
        "box-sizing:border-box}</style>安全验证</body></html>"
    )
    assert fetch_module.looks_like_challenge_page(challenge)
    real_page = (
        "<html><head><title>犀角鸟 - 洛克王国世界WIKI_BWIKI</title></head>"
        "<body>" + "x" * 100000 + "</body></html>"
    )
    assert not fetch_module.looks_like_challenge_page(real_page)


def test_urllib_fetcher_raises_on_challenge_page(monkeypatch) -> None:
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @property
        def headers(self):
            return self

        def get_content_charset(self):
            return "utf-8"

        def items(self):
            return []

        def read(self):
            return b"<html><head><title></title></head><body><style>*{margin:0;padding:0;box-sizing:border-box}</style>safe check</body></html>"

    class FakeCompleted:
        returncode = 0
        stdout = "<html><head><title></title></head><body><style>*{margin:0;padding:0;box-sizing:border-box}</style>safe check</body></html>"
        stderr = ""

    def fake_urlopen(request, timeout):
        return FakeResponse()

    def fake_run(command, **kwargs):
        return FakeCompleted()

    monkeypatch.setattr(fetch_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(fetch_module.subprocess, "run", fake_run)
    from urllib.error import URLError

    try:
        fetch_module.UrllibFetcher().fetch("https://example.com/x")
    except URLError as exc:
        assert "WAF" in str(exc)
    else:
        raise AssertionError("challenge page must raise URLError, never return a response")


def test_incremental_fetch_delay_seconds_paces_requests(tmp_path: Path) -> None:
    import time

    body = _raw_template("测试宠物A", "101")
    fetcher = FakeFetcher(
        {
            "https://example.com/pets/测试宠物A": FetchResponse(status=200, headers={}, body=body),
            "https://example.com/pets/测试宠物B": FetchResponse(
                status=200, headers={}, body=_raw_template("测试宠物B", "102")
            ),
            "https://example.com/pets/测试宠物C": FetchResponse(
                status=200, headers={}, body=_raw_template("测试宠物C", "103")
            ),
        }
    )
    start = time.monotonic()
    incremental_fetch(
        _targets([("测试宠物A", "101"), ("测试宠物B", "102"), ("测试宠物C", "103")]),
        previous=None,
        fetcher=fetcher,
        output_dir=tmp_path,
        quarantine_dir=tmp_path / "q",
        parser_version=6,
        delay_seconds=0.3,
    )
    elapsed = time.monotonic() - start
    # 3 targets -> 2 inter-request delays; generous lower bound against CI jitter.
    assert elapsed >= 0.4
    assert elapsed < 10
    assert len(fetcher.calls) == 3


def test_classify_content_hash_match_is_unchanged() -> None:
    body = _raw_template("测试宠物A")
    response = FetchResponse(status=200, headers={}, body=body)
    assert classify(_body_sha(body), response) == "unchanged"


def test_classify_content_hash_change_is_changed() -> None:
    body = _raw_template("测试宠物A")
    response = FetchResponse(status=200, headers={}, body=body)
    assert classify(_body_sha(body + " "), response) == "changed"


def test_classify_error_status_is_error() -> None:
    assert classify(None, FetchResponse(status=500, headers={}, body="")) == "error"
    assert classify("abc", FetchResponse(status=404, headers={}, body="")) == "error"


def test_classify_without_previous_sha_is_changed() -> None:
    assert classify(None, FetchResponse(status=200, headers={}, body="x")) == "changed"


def test_conditional_headers_include_etag_and_last_modified() -> None:
    headers = conditional_headers("W/abc", "Mon, 01 Jan 2026 00:00:00 GMT")
    assert headers["If-None-Match"] == "W/abc"
    assert headers["If-Modified-Since"] == "Mon, 01 Jan 2026 00:00:00 GMT"


def test_conditional_headers_partial() -> None:
    assert conditional_headers("W/abc", None) == {"If-None-Match": "W/abc"}
    assert conditional_headers(None, "date") == {"If-Modified-Since": "date"}


def test_conditional_headers_empty() -> None:
    assert conditional_headers(None, None) == {}


def test_incremental_fetch_all_304_zero_writes_zero_parses(tmp_path: Path, monkeypatch) -> None:
    body_a = _raw_template("测试宠物A", "101")
    body_b = _raw_template("测试宠物B", "102")
    previous = _make_manifest(
        {"101-测试宠物A.json": body_a, "102-测试宠物B.json": body_b},
        etags={"101-测试宠物A.json": "eta", "102-测试宠物B.json": "etb"},
    )
    responses = {
        "https://example.com/pets/测试宠物A": FetchResponse(status=304, headers={}, body=""),
        "https://example.com/pets/测试宠物B": FetchResponse(status=304, headers={}, body=""),
    }
    fetcher = FakeFetcher(responses)
    parse_calls: list[str] = []

    def counting_parse(source_url: str, html: str) -> dict:
        parse_calls.append(source_url)
        return fetch_module.parse_pet_detail(source_url, html)

    monkeypatch.setattr(fetch_module, "parse_pet_detail", counting_parse)
    outcome = incremental_fetch(
        _targets([("测试宠物A", "101"), ("测试宠物B", "102")]),
        previous=previous,
        fetcher=fetcher,
        output_dir=tmp_path / "staging",
        quarantine_dir=tmp_path / "quarantine",
        parser_version=6,
    )

    assert outcome.errors == []
    assert outcome.change_set["unchanged"] == ["测试宠物A", "测试宠物B"]
    assert outcome.change_set["added"] == [] and outcome.change_set["modified"] == []
    assert parse_calls == []  # zero parses
    assert not (tmp_path / "staging").exists() or not list((tmp_path / "staging").glob("*"))


def test_incremental_fetch_200_same_hash_no_write(tmp_path: Path) -> None:
    body = _raw_template("测试宠物A", "101")
    previous = _make_manifest({"101-测试宠物A.json": body})
    # Server ignores conditional requests and always returns 200.
    fetcher = FakeFetcher(
        {"https://example.com/pets/测试宠物A": FetchResponse(status=200, headers={}, body=body)}
    )
    outcome = incremental_fetch(
        _targets([("测试宠物A", "101")]),
        previous=previous,
        fetcher=fetcher,
        output_dir=tmp_path / "staging",
        quarantine_dir=tmp_path / "quarantine",
        parser_version=6,
    )
    assert outcome.change_set["unchanged"] == ["测试宠物A"]
    assert not (tmp_path / "staging").exists() or not list((tmp_path / "staging").glob("*"))


def test_incremental_fetch_changed_body_writes_and_marks_modified(tmp_path: Path) -> None:
    old_body = _raw_template("测试宠物A", "101")
    new_body = _raw_template("测试宠物A", "101", extra="|精灵描述=新版描述")
    previous = _make_manifest({"101-测试宠物A.json": old_body})
    fetcher = FakeFetcher(
        {
            "https://example.com/pets/测试宠物A": FetchResponse(
                status=200, headers={"etag": '"v2"'}, body=new_body
            )
        }
    )
    outcome = incremental_fetch(
        _targets([("测试宠物A", "101")]),
        previous=previous,
        fetcher=fetcher,
        output_dir=tmp_path / "staging",
        quarantine_dir=tmp_path / "quarantine",
        parser_version=6,
    )
    assert outcome.change_set["modified"] == ["测试宠物A"]
    written = tmp_path / "staging" / "101-测试宠物A.json"
    assert written.exists()
    import json

    detail = json.loads(written.read_text(encoding="utf-8"))
    assert detail["name"] == "测试宠物A"
    assert detail["profile"]["编号"] == "101"
    assert outcome.etags["101-测试宠物A.json"] == '"v2"'


def test_incremental_fetch_new_target_is_added(tmp_path: Path) -> None:
    body = _raw_template("测试宠物F", "106")
    fetcher = FakeFetcher(
        {"https://example.com/pets/测试宠物F": FetchResponse(status=200, headers={}, body=body)}
    )
    outcome = incremental_fetch(
        _targets([("测试宠物F", "106")]),
        previous=None,
        fetcher=fetcher,
        output_dir=tmp_path / "staging",
        quarantine_dir=tmp_path / "quarantine",
        parser_version=6,
    )
    assert outcome.change_set["added"] == ["测试宠物F"]
    assert (tmp_path / "staging" / "106-测试宠物F.json").exists()


def test_incremental_fetch_parallel_workers_match_serial(tmp_path: Path) -> None:
    names = [(f"宠物{n:02d}", f"{200 + n}") for n in range(6)]
    responses = {
        f"https://example.com/pets/{name}": FetchResponse(
            status=200, headers={}, body=_raw_template(name, num)
        )
        for name, num in names
    }
    serial = incremental_fetch(
        _targets(names),
        previous=None,
        fetcher=FakeFetcher(dict(responses)),
        output_dir=tmp_path / "serial",
        quarantine_dir=tmp_path / "quarantine",
        parser_version=6,
        workers=1,
    )
    parallel = incremental_fetch(
        _targets(names),
        previous=None,
        fetcher=FakeFetcher(dict(responses)),
        output_dir=tmp_path / "parallel",
        quarantine_dir=tmp_path / "quarantine",
        parser_version=6,
        workers=3,
    )
    assert parallel.change_set == serial.change_set
    assert parallel.errors == serial.errors
    assert sorted(p.name for p in (tmp_path / "serial").glob("*.json")) == sorted(
        p.name for p in (tmp_path / "parallel").glob("*.json")
    )


def test_incremental_fetch_parse_failure_quarantines(tmp_path: Path) -> None:
    # A page that parses to an empty pet name fails schema validation (name
    # must be non-empty) and lands in quarantine.
    bad_html = "<html><body><p>not a pet page</p></body></html>"
    fetcher = FakeFetcher(
        {"https://example.com/pets/测试宠物A": FetchResponse(status=200, headers={}, body=bad_html)}
    )
    outcome = incremental_fetch(
        _targets([("测试宠物A", "101")]),
        previous=None,
        fetcher=fetcher,
        output_dir=tmp_path / "staging",
        quarantine_dir=tmp_path / "quarantine",
        parser_version=6,
    )
    assert outcome.change_set["added"] == []
    assert any(name == "测试宠物A" for name, _ in outcome.errors)
    assert (tmp_path / "quarantine" / "测试宠物A.json").exists()
    assert (tmp_path / "quarantine" / "测试宠物A.error.json").exists()
    assert not (tmp_path / "staging").exists() or not list((tmp_path / "staging").glob("*"))


def test_incremental_fetch_errors_do_not_interrupt_other_targets(tmp_path: Path) -> None:
    good_body = _raw_template("测试宠物B", "102")
    fetcher = FakeFetcher(
        {
            "https://example.com/pets/测试宠物A": FetchResponse(status=500, headers={}, body=""),
            "https://example.com/pets/测试宠物B": FetchResponse(
                status=200, headers={}, body=good_body
            ),
        }
    )
    outcome = incremental_fetch(
        _targets([("测试宠物A", "101"), ("测试宠物B", "102")]),
        previous=None,
        fetcher=fetcher,
        output_dir=tmp_path / "staging",
        quarantine_dir=tmp_path / "quarantine",
        parser_version=6,
    )
    assert any(name == "测试宠物A" and "500" in error for name, error in outcome.errors)
    assert outcome.change_set["added"] == ["测试宠物B"]
    assert (tmp_path / "staging" / "102-测试宠物B.json").exists()


def test_incremental_fetch_force_skips_304_and_conditional_headers(tmp_path: Path) -> None:
    body = _raw_template("测试宠物A", "101")
    previous = _make_manifest({"101-测试宠物A.json": body}, etags={"101-测试宠物A.json": "eta"})
    fetcher = FakeFetcher(
        {"https://example.com/pets/测试宠物A": FetchResponse(status=200, headers={}, body=body)}
    )
    outcome = incremental_fetch(
        _targets([("测试宠物A", "101")]),
        previous=previous,
        fetcher=fetcher,
        output_dir=tmp_path / "staging",
        quarantine_dir=tmp_path / "quarantine",
        parser_version=6,
        force=True,
    )
    # No conditional headers were sent; the file was rewritten even though the
    # body hash matches the previous entry.
    _, sent_headers = fetcher.calls[0]
    assert not sent_headers or "If-None-Match" not in sent_headers
    assert outcome.change_set["modified"] == ["测试宠物A"]
    assert (tmp_path / "staging" / "101-测试宠物A.json").exists()


def test_incremental_fetch_removed_files_from_previous(tmp_path: Path) -> None:
    body_a = _raw_template("测试宠物A", "101")
    body_b = _raw_template("测试宠物B", "102")
    previous = _make_manifest(
        {"101-测试宠物A.json": body_a, "102-测试宠物B.json": body_b},
        etags={"101-测试宠物A.json": "eta", "102-测试宠物B.json": "etb"},
    )
    # Only 测试宠物A is still listed in the index; 测试宠物B disappeared.
    fetcher = FakeFetcher(
        {"https://example.com/pets/测试宠物A": FetchResponse(status=304, headers={}, body="")}
    )
    outcome = incremental_fetch(
        _targets([("测试宠物A", "101")]),
        previous=previous,
        fetcher=fetcher,
        output_dir=tmp_path / "staging",
        quarantine_dir=tmp_path / "quarantine",
        parser_version=6,
    )
    assert outcome.change_set["removed"] == ["102-测试宠物B.json"]
    assert outcome.change_set["unchanged"] == ["测试宠物A"]


def test_parser_version_does_not_drive_skipping(tmp_path: Path) -> None:
    """--min-parser-version only affects written metadata, never the skip decision."""
    body = _raw_template("测试宠物A", "101")
    previous = _make_manifest({"101-测试宠物A.json": body}, etags={"101-测试宠物A.json": "eta"})
    previous.entries["101-测试宠物A.json"].parser_version = 1  # outdated file
    fetcher = FakeFetcher(
        {"https://example.com/pets/测试宠物A": FetchResponse(status=304, headers={}, body="")}
    )
    outcome = incremental_fetch(
        _targets([("测试宠物A", "101")]),
        previous=previous,
        fetcher=fetcher,
        output_dir=tmp_path / "staging",
        quarantine_dir=tmp_path / "quarantine",
        parser_version=6,
    )
    assert outcome.change_set["unchanged"] == ["测试宠物A"]
    assert outcome.change_set["modified"] == []
    assert not (tmp_path / "staging").exists() or not list((tmp_path / "staging").glob("*"))
