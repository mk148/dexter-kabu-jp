#!/usr/bin/env python3
"""
pead_candidates.py — Signal E 全候補の検証用台帳を作成・更新する。

Usage:
    python3 src/skills/market-distortion-scan/pead_candidates.py collect
    python3 src/skills/market-distortion-scan/pead_candidates.py collect --date 20260513 --pead-days 5
    python3 src/skills/market-distortion-scan/pead_candidates.py list
    python3 src/skills/market-distortion-scan/pead_candidates.py stats

Notes:
    - tracker.json は推奨候補だけを記録する本命台帳。
    - pead_candidates.json はPEAD検証用に、scan_pead が通した全候補を保存する。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from pead import prev_biz_days, scan_pead

SKILL_DIR = Path(__file__).parent
LEDGER_FILE = SKILL_DIR / "results" / "pead_candidates.json"


def load_ledger() -> list[dict]:
    if not LEDGER_FILE.exists():
        return []
    return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))


def save_ledger(records: list[dict]) -> None:
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_scan_date(raw: str | None) -> str:
    if raw is None:
        return date.today().strftime("%Y%m%d")
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw.replace("-", "")
    if len(raw) == 8 and raw.isdigit():
        return raw
    raise ValueError("--date は YYYYMMDD または YYYY-MM-DD 形式で指定してください")


def yyyymmdd_to_iso(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:]}"


def record_key(record: dict) -> tuple[str, str, str]:
    return (
        str(record.get("event_date", "")),
        str(record.get("code", "")),
        str(record.get("title", "")),
    )


def signal_to_record(signal: dict, collected_at: str) -> dict:
    event_date = str(signal.get("date", ""))
    return {
        "event_date": yyyymmdd_to_iso(event_date) if len(event_date) == 8 else event_date,
        "collected_at": collected_at,
        "signal": "E",
        "code": signal.get("code", ""),
        "company": signal.get("company", ""),
        "title": signal.get("title", ""),
        "time": signal.get("time", ""),
        "pead_score": signal.get("pead_score"),
        "composite_surprise": signal.get("composite_surprise"),
        "text_score": signal.get("text_score"),
        "current_change_pct": signal.get("current_change_pct"),
        "score": signal.get("score"),
        "reason": signal.get("reason", ""),
        "tdnet_url": signal.get("tdnet_url", ""),
        "returns": {},
    }


def collect(scan_date: str, pead_days: int, dry_run: bool) -> int:
    dates = prev_biz_days(pead_days, from_date=scan_date)
    print(f"PEAD候補取得: scan_date={scan_date} dates={','.join(dates)}", file=sys.stderr)
    signals = scan_pead(dates)
    collected_at = date.today().isoformat()
    new_records = [signal_to_record(signal, collected_at) for signal in signals]

    records = load_ledger()
    existing = {record_key(record) for record in records}
    additions = [record for record in new_records if record_key(record) not in existing]

    print(f"候補: {len(new_records)}件 / 新規: {len(additions)}件 / 既存: {len(new_records) - len(additions)}件")
    for record in additions[:20]:
        print(
            f"  {record['event_date']} [{record['code']}] {record['company']} "
            f"pead={record['pead_score']} composite={record['composite_surprise']} "
            f"text={record['text_score']} {record['reason']}"
        )
    if len(additions) > 20:
        print(f"  ... and {len(additions) - 20} more")

    if dry_run:
        print("dry-run: pead_candidates.json は更新していません")
        return 0

    if additions:
        records.extend(additions)
        records.sort(key=lambda rec: (rec.get("event_date", ""), rec.get("code", ""), rec.get("title", "")))
        save_ledger(records)
        print(f"保存: {LEDGER_FILE}")
    else:
        print("新規保存なし")
    return 0


def print_records(limit: int | None) -> int:
    records = load_ledger()
    if not records:
        print("PEAD候補台帳は空です。")
        return 0
    rows = records[-limit:] if limit else records
    print("date       code company          pead  comp  text  chg  title")
    print("-" * 96)
    for rec in rows:
        print(
            f"{rec.get('event_date', ''):<10} "
            f"{rec.get('code', ''):<4} "
            f"{str(rec.get('company', ''))[:14]:<14} "
            f"{rec.get('pead_score', 'NA')!s:>5} "
            f"{rec.get('composite_surprise', 'NA')!s:>5} "
            f"{rec.get('text_score', 'NA')!s:>5} "
            f"{rec.get('current_change_pct', 'NA')!s:>5} "
            f"{rec.get('title', '')}"
        )
    return 0


def print_stats() -> int:
    records = load_ledger()
    by_date: dict[str, int] = {}
    for rec in records:
        key = str(rec.get("event_date", ""))
        by_date[key] = by_date.get(key, 0) + 1

    print(f"総PEAD候補: {len(records)}件")
    for key in sorted(by_date):
        print(f"  {key}: {by_date[key]}件")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Signal E 全候補の検証用台帳")
    sub = parser.add_subparsers(dest="command", required=True)

    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--date", help="基準日 YYYYMMDD または YYYY-MM-DD。省略時は今日")
    collect_parser.add_argument("--pead-days", type=int, default=1)
    collect_parser.add_argument("--dry-run", action="store_true")

    list_parser = sub.add_parser("list")
    list_parser.add_argument("--limit", type=int)

    sub.add_parser("stats")

    args = parser.parse_args()
    if args.command == "collect":
        if args.pead_days < 1:
            print("ERROR: --pead-days は 1 以上で指定してください", file=sys.stderr)
            return 2
        scan_date = normalize_scan_date(args.date)
        return collect(scan_date, args.pead_days, args.dry_run)
    if args.command == "list":
        return print_records(args.limit)
    if args.command == "stats":
        return print_stats()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
