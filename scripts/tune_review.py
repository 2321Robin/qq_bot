"""本地调优复核工具（S7-AUTO-P2-11）。

读取 data/auto_chat_tune.jsonl（自主回复的上文+回复记录），在浏览器里
逐条标注「好 / 坏」，标注即时保存到 data/auto_chat_tune_labels.json，
并可导出 CSV。

用法：
    python scripts/tune_review.py               # 默认文件与端口
    python scripts/tune_review.py 其他.jsonl 9000

然后浏览器打开 http://127.0.0.1:8788

仅用标准库，不需要安装任何依赖；只监听 127.0.0.1，不对外暴露。
"""

from __future__ import annotations

import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TUNE = ROOT / "data" / "auto_chat_tune.jsonl"
LABELS_FILE = ROOT / "data" / "auto_chat_tune_labels.json"
PORT = 8788

_PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>自主回复调优复核</title>
<style>
  body { font-family: "Microsoft YaHei", sans-serif; background:#f5f6f7; margin:0; }
  header { position:sticky; top:0; background:#1f2733; color:#fff; padding:10px 18px;
           display:flex; gap:18px; align-items:center; }
  header b { font-size:16px; }
  .stat { background:#2f3b4c; border-radius:6px; padding:4px 10px; font-size:13px; }
  .good { color:#7dd87d; } .bad { color:#ff8a80; }
  main { max-width:860px; margin:16px auto; padding:0 12px; }
  .card { background:#fff; border-radius:10px; padding:12px 16px; margin-bottom:14px;
          box-shadow:0 1px 3px rgba(0,0,0,.08); }
  .meta { color:#7a8494; font-size:12px; margin-bottom:6px; }
  .path { display:inline-block; background:#eef3fb; color:#3a6ea5; border-radius:4px;
          padding:1px 8px; margin-right:8px; font-size:12px; }
  .ctx { background:#fafbfc; border-left:3px solid #d8dee6; padding:6px 10px;
         font-size:13px; color:#4a5568; white-space:pre-wrap; }
  .trig { color:#b06f2d; font-size:13px; margin:6px 0; }
  .reply { font-size:15px; margin:8px 0; white-space:pre-wrap; }
  .btns button { border:0; border-radius:6px; padding:6px 18px; margin-right:8px;
                 cursor:pointer; font-size:13px; }
  .bg { background:#e5f4e5; color:#217a21; } .bb { background:#fde7e5; color:#b3261e; }
  .bc { background:#eee; color:#555; }
  button.on { outline:2px solid #1f2733; }
  .id { color:#b9c0ca; font-size:11px; float:right; }
</style>
</head>
<body>
<header>
  <b>自主回复调优复核</b>
  <span class="stat">总数 <span id="n-all">0</span></span>
  <span class="stat good">好 <span id="n-good">0</span></span>
  <span class="stat bad">坏 <span id="n-bad">0</span></span>
  <span class="stat">未标注 <span id="n-none">0</span></span>
  <a href="/export" style="color:#9fc3f5;font-size:13px">导出 CSV</a>
</header>
<main id="list"></main>
<script>
const ENTRIES = __ENTRIES__;
const LABELS = __LABELS__;

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }

function render() {
  const list = document.getElementById("list");
  let good = 0, bad = 0;
  list.innerHTML = ENTRIES.map(e => {
    const label = LABELS[e.id] || "";
    if (label === "good") good++;
    if (label === "bad") bad++;
    const ctx = (e.context || []).map(c =>
      (c.bot_reply ? "机器人：" : "用户" + c.user + "：") + c.text).join("\\n");
    const btn = (cls, val, text) =>
      `<button class="${cls} ${label === val ? "on" : ""}" onclick="mark('${e.id}','${val}')">${text}</button>`;
    return `<div class="card">
      <div class="id">${e.id}</div>
      <div class="meta"><span class="path">${esc(e.path)}</span>${esc(e.ts)} · 群${e.group_id}</div>
      <div class="ctx">${esc(ctx) || "（无上文）"}</div>
      <div class="trig">触发：${esc(e.trigger)}</div>
      <div class="reply">【回复】${esc((e.reply || []).join(" ／ "))}</div>
      <div class="btns">${btn("bg", "good", "👍 好")}${btn("bb", "bad", "👎 坏")}${btn("bc", "", "清除")}</div>
    </div>`;
  }).join("");
  document.getElementById("n-all").textContent = ENTRIES.length;
  document.getElementById("n-good").textContent = good;
  document.getElementById("n-bad").textContent = bad;
  document.getElementById("n-none").textContent = ENTRIES.length - good - bad;
}

function mark(id, label) {
  fetch("/label", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, label: label || null }),
  }).then(() => { LABELS[id] = label || undefined; render(); });
}

render();
</script>
</body>
</html>
"""


def entry_id(entry: dict) -> str:
    raw = entry.get("ts", "") + "|" + "|".join(entry.get("reply", []))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def load_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        entry["id"] = entry_id(entry)
        entries.append(entry)
    return entries


def load_labels() -> dict:
    if not LABELS_FILE.exists():
        return {}
    try:
        return json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_labels(labels: dict) -> None:
    LABELS_FILE.write_text(
        json.dumps(labels, ensure_ascii=False, indent=0), encoding="utf-8"
    )


def export_csv(entries: list[dict], labels: dict) -> str:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "标注", "时间", "路径", "触发", "回复", "上文"])
    for entry in entries:
        writer.writerow(
            [
                entry["id"],
                labels.get(entry["id"], ""),
                entry.get("ts", ""),
                entry.get("path", ""),
                entry.get("trigger", ""),
                " ／ ".join(entry.get("reply", [])),
                " ⏎ ".join(
                    f"用户{c.get('user')}：{c.get('text')}" for c in entry.get("context", [])
                ),
            ]
        )
    return buf.getvalue()


def make_handler(entries: list[dict]) -> type[BaseHTTPRequestHandler]:
    labels_box = {"data": load_labels()}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                page = _PAGE.replace("__ENTRIES__", json.dumps(entries, ensure_ascii=False))
                page = page.replace("__LABELS__", json.dumps(labels_box["data"], ensure_ascii=False))
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/export":
                csv_text = export_csv(entries, labels_box["data"])
                body = b"\xef\xbb\xbf" + csv_text.encode("utf-8")  # BOM 让 Excel 识别 UTF-8
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition", "attachment; filename=tune_labels.csv"
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/label":
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            entry_id = payload.get("id", "")
            label = payload.get("label")
            if label:
                labels_box["data"][entry_id] = label
            else:
                labels_box["data"].pop(entry_id, None)
            save_labels(labels_box["data"])
            self._send(200, b'{"ok": true}', "application/json")

        def log_message(self, *args: object) -> None:
            pass  # 静默访问日志

    return Handler


def main() -> None:
    tune_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TUNE
    entries = load_entries(tune_path)
    server = HTTPServer(("127.0.0.1", PORT), make_handler(entries))
    print(f"已加载 {len(entries)} 条记录：{tune_path}")
    print(f"复核页面：http://127.0.0.1:{PORT}  （Ctrl+C 退出）")
    print(f"标注保存到：{LABELS_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
