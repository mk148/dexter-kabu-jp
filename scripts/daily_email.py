#!/usr/bin/env python3
"""Build and send a compact, mobile-friendly daily-check email."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import unicodedata
from email.message import EmailMessage
from pathlib import Path

DISPLAY_WIDTH = 32
INVALID_COMPANY_NAMES = {"", "株主", "通常", "【"}


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in {"F", "W", "A"} else 1 for ch in text)


def wrap_display(text: str, width: int = DISPLAY_WIDTH) -> list[str]:
    words = re.split(r"\s+", str(text).strip())
    lines: list[str] = []
    current = ""
    for word in words:
        chunks: list[str] = []
        chunk = ""
        for char in word:
            if chunk and display_width(chunk + char) > width:
                chunks.append(chunk)
                chunk = char
            else:
                chunk += char
        if chunk:
            chunks.append(chunk)
        for piece in chunks:
            candidate = f"{current} {piece}".strip()
            if current and display_width(candidate) > width:
                lines.append(current)
                current = piece
            else:
                current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def load_records(path: str) -> list[dict]:
    file = Path(path)
    if not file.exists():
        return []
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def record_key(record: dict, kind: str) -> tuple[str, ...]:
    if kind == "distortion":
        return (
            str(record.get("date", "")),
            str(record.get("code", "")),
            str(record.get("signal", "")),
        )
    return (str(record.get("code", "")), str(record.get("source_url", "")))


def added_records(before: list[dict], after: list[dict], kind: str) -> list[dict]:
    before_keys = {record_key(record, kind) for record in before}
    return [record for record in after if record_key(record, kind) not in before_keys]


def missing_identity(records: list[dict]) -> list[dict]:
    return [
        record
        for record in records
        if not str(record.get("code") or "").strip()
        or str(record.get("company") or record.get("name") or "").strip() in INVALID_COMPANY_NAMES
    ]


def warning_lines(*log_paths: str) -> list[str]:
    warnings: list[str] = []
    pattern = re.compile(r"WARN|ERROR|失敗|failed", re.IGNORECASE)
    for path in log_paths:
        file = Path(path)
        if not file.exists():
            continue
        for raw_line in file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if line and pattern.search(line):
                warnings.append(line)
    return warnings[-8:]


def _truncate_display(text: str, max_width: int) -> str:
    result = ""
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in {"F", "W", "A"} else 1
        if display_width(result) + w > max_width:
            return result + "…"
        result += ch
    return result


def compact_record(record: dict, kind: str) -> list[str]:
    code = str(record.get("code") or "").strip()
    company = str(record.get("company") or record.get("name") or "").strip()
    lines = [f"{code} {company}"]
    if kind == "distortion":
        price = float(record.get("price_at_signal") or 0)
        lines.append(f"S:{record.get('signal', '-')} ¥{price:,.0f}")
    else:
        yld = float(record.get("total_yield_pct") or 0)
        benefit = _truncate_display(str(record.get("benefit_content") or "").strip(), 16)
        lines.append(f"{yld:.1f}%" + (f" {benefit}" if benefit else ""))
    return lines


def build_body(
    status: str,
    distortion_before: list[dict],
    distortion_after: list[dict],
    yutai_before: list[dict],
    yutai_after: list[dict],
    warnings: list[str],
) -> str:
    distortion_new = added_records(distortion_before, distortion_after, "distortion")
    yutai_new = added_records(yutai_before, yutai_after, "yutai")
    missing = missing_identity(distortion_after) + missing_identity(yutai_after)

    raw_lines = [
        "日次チェック",
        "",
        status,
        f"歪み {len(distortion_after)}件 新{len(distortion_new)}"
        f" / 優待 {len(yutai_after)}件 新{len(yutai_new)}",
    ]
    if missing:
        raw_lines.append(f"欠損 {len(missing)}件")

    raw_lines.extend(["", "▼歪み新規"])
    if distortion_new:
        for record in distortion_new:
            raw_lines.extend(compact_record(record, "distortion"))
    else:
        raw_lines.append("なし")

    raw_lines.extend(["", "▼優待新規"])
    if yutai_new:
        for record in yutai_new:
            raw_lines.extend(compact_record(record, "yutai"))
    else:
        raw_lines.append("なし")

    if warnings:
        raw_lines.extend(["", "▼警告"])
        raw_lines.extend(_truncate_display(w, DISPLAY_WIDTH) for w in warnings)

    wrapped: list[str] = []
    for line in raw_lines:
        wrapped.extend(wrap_display(line) if line else [""])
    return "\n".join(wrapped).strip() + "\n"


def build_html(body: str, run_url: str, repository: str) -> str:
    tracker_url = f"https://github.com/{repository}/blob/main/src/skills/market-distortion-scan/results/tracker.json"
    yutai_url = f"https://github.com/{repository}/blob/main/src/skills/kasumi-yutai-yield/results/kasumi_yutai_ledger.json"
    escaped = html.escape(body).replace("\n", "<br>\n")
    return f"""<!doctype html>
<html lang="ja"><body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:16px;line-height:1.55;max-width:36em;margin:0 auto;padding:12px">
<div>{escaped}</div>
<p><a href="{html.escape(run_url)}">GitHub Actionsの実行結果</a></p>
<p><a href="{tracker_url}">歪み台帳</a><br><a href="{yutai_url}">優待台帳</a></p>
</body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--distortion-before", required=True)
    parser.add_argument("--distortion-after", required=True)
    parser.add_argument("--yutai-before", required=True)
    parser.add_argument("--yutai-after", required=True)
    parser.add_argument("--distortion-log", required=True)
    parser.add_argument("--yutai-log", required=True)
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    status = os.environ.get("JOB_STATUS", "unknown")
    run_url = os.environ.get("RUN_URL", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    body = build_body(
        status,
        load_records(args.distortion_before),
        load_records(args.distortion_after),
        load_records(args.yutai_before),
        load_records(args.yutai_after),
        warning_lines(args.distortion_log, args.yutai_log),
    )
    print(body)

    if not args.send:
        return 0

    username = os.environ["SMTP_USERNAME"]
    message = EmailMessage()
    message["Subject"] = f"[株チェック] {status}"
    message["From"] = username
    message["To"] = "daiyum@gmail.com"
    wrapped_url = "\n".join(wrap_display(run_url))
    message.set_content(body + f"\n実行結果:\n{wrapped_url}\n")
    message.add_alternative(build_html(body, run_url, repository), subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(username, os.environ["SMTP_APP_PASSWORD"])
        smtp.send_message(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
