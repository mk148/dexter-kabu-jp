#!/usr/bin/env python3
"""歪みシグナル銘柄のうち総合利回り5%以上を優待台帳へ追加する。"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path


SKILL_DIR = Path(__file__).parent
LEDGER_FILE = SKILL_DIR / "results" / "kasumi_yutai_ledger.json"
MARKET_SCAN_DIR = SKILL_DIR.parent / "market-distortion-scan"
YIELD_SCRIPT = Path.home() / ".codex/skills/stock-yield/scripts/fetch_yield.py"
MIN_TOTAL_YIELD = 5.0
SOURCE = "market-distortion-signal"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def signal_price(item: dict) -> float:
    source = item.get("source") or {}
    financials = source.get("financials") or item.get("financials") or {}
    for value in (source.get("price"), financials.get("price"), source.get("price_at_signal")):
        try:
            if value is not None and float(value) > 0:
                return float(value)
        except (TypeError, ValueError):
            pass
    return 0.0


def read_signal_targets(scan_path: Path) -> list[dict]:
    # 記録対象の定義を market-distortion 側と共有する。
    sys.path.insert(0, str(MARKET_SCAN_DIR))
    import record_signals  # type: ignore

    targets = record_signals.extract_targets(load_json(scan_path))
    result = []
    seen: set[str] = set()
    for target in targets:
        code = str(target.get("code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        result.append({
            "code": code,
            "name": str(target.get("company") or code),
            "price": signal_price(target),
            "holdings": 100,
        })
    return result


def fetch_yields(stocks: list[dict]) -> list[dict]:
    if not stocks:
        return []
    if not YIELD_SCRIPT.exists():
        raise FileNotFoundError(f"総合利回り取得スクリプトがありません: {YIELD_SCRIPT}")
    with tempfile.TemporaryDirectory(prefix="signal_yutai_") as tmp:
        input_path = Path(tmp) / "stocks.json"
        output_path = Path(tmp) / "yield.csv"
        input_path.write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")
        cmd = [sys.executable, str(YIELD_SCRIPT), str(input_path), str(output_path)]
        result = subprocess.run(cmd, text=True)
        if result.returncode != 0:
            result = subprocess.run(cmd, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"総合利回り取得に失敗しました: exit={result.returncode}")
        with output_path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))


def merge_into_ledger(results: list[dict], scan_date: str) -> tuple[int, int, list[dict]]:
    ledger = load_json(LEDGER_FILE) if LEDGER_FILE.exists() else []
    # 同じシグナル取得元・コードは更新し、記事由来の既存レコードは保持する。
    by_key = {(str(r.get("code") or ""), str(r.get("source") or "")): r for r in ledger}
    added = updated = 0
    qualifying = []
    for row in results:
        try:
            total = float(row.get("total_yield") or 0)
        except (TypeError, ValueError):
            total = 0.0
        if total < MIN_TOTAL_YIELD:
            continue
        code = str(row.get("code") or "")
        qualifying.append(row)
        key = (code, SOURCE)
        record = {
            "date": scan_date,
            "code": code,
            "company": row.get("name") or code,
            "total_yield_pct": total,
            "source_title": "歪みシグナル連携（総合利回り5%以上）",
            "source_url": f"https://kabutan.jp/stock/yutai?code={code}",
            "matched_text": f"配当{float(row.get('div_yield') or 0):.2f}% + 優待{float(row.get('yutai_yield') or 0):.2f}%",
            "benefit_content": row.get("yutai_note") or "優待内容は取得元ページを確認",
            "status": "watch",
            "source": SOURCE,
            "holding_status": "",
        }
        existing = by_key.get(key)
        if existing is None:
            ledger.append(record)
            by_key[key] = record
            added += 1
        else:
            changed = False
            for field, value in record.items():
                if value and existing.get(field) != value and field != "holding_status":
                    existing[field] = value
                    changed = True
            if changed:
                updated += 1
    if added or updated:
        LEDGER_FILE.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    return added, updated, qualifying


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-json", required=True)
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()
    targets = read_signal_targets(Path(args.from_json))
    print(f"優待照合対象: {len(targets)}銘柄")
    results = fetch_yields(targets)
    added, updated, qualifying = merge_into_ledger(results, args.date)
    print(f"総合利回り5%以上: {len(qualifying)}銘柄 / 新規追加: {added}件 / 更新: {updated}件")
    for row in qualifying:
        print(f"  {row.get('code')} {row.get('name')} 合計{float(row.get('total_yield') or 0):.2f}% [{row.get('status')}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
