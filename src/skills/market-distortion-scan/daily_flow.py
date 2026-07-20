#!/usr/bin/env python3
"""
daily_flow.py — 歪みチェック -> 台帳記録 -> 損益チェックを一括実行する。

Usage:
    python3 src/skills/market-distortion-scan/daily_flow.py
    python3 src/skills/market-distortion-scan/daily_flow.py --date 20260625
    python3 src/skills/market-distortion-scan/daily_flow.py --loop --interval-minutes 1440
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SKILL_DIR = Path(__file__).parent
TRACKER_FILE = SKILL_DIR / "results" / "tracker.json"

EXCLUDE_TITLE_KEYWORDS = (
    "取得結果",
    "取得終了",
    "取得状況",
    "進捗状況",
    "消却",
    "処分",
    "連結子会社",
    "子会社",
)


def run_step(label: str, cmd: list[str], stdout_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    print(f"\n## {label}")
    print("$ " + " ".join(cmd))
    if stdout_path:
        with stdout_path.open("w", encoding="utf-8") as out:
            result = subprocess.run(cmd, stdout=out, stderr=subprocess.PIPE, text=True)
    else:
        result = subprocess.run(cmd, text=True)
    if result.returncode != 0 and result.stderr:
        print(result.stderr[-2000:], file=sys.stderr)
    return result


def retry_step(label: str, cmd: list[str], stdout_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = run_step(label, cmd, stdout_path)
    if result.returncode == 0:
        return result
    print(f"\n[WARN] {label} failed. Retrying once with the same command.")
    return run_step(f"{label} retry", cmd, stdout_path)


def load_tracker_ids() -> set[int]:
    if not TRACKER_FILE.exists():
        return set()
    records = json.loads(TRACKER_FILE.read_text(encoding="utf-8"))
    return {int(rec.get("id", 0)) for rec in records if rec.get("id") is not None}


def load_tracker_records() -> list[dict]:
    if not TRACKER_FILE.exists():
        return []
    return json.loads(TRACKER_FILE.read_text(encoding="utf-8"))


def title_exclusion_reason(item: dict) -> str | None:
    title = str(item.get("title") or "")
    for keyword in EXCLUDE_TITLE_KEYWORDS:
        if keyword in title:
            return keyword
    return None


def should_keep_signal_item(signal: str, item: dict) -> tuple[bool, str | None]:
    if signal == "signal_c":
        reason = title_exclusion_reason(item)
        if reason:
            return False, reason
    if signal == "signal_e" and item.get("pead_score", 0) < 70:
        return False, "pead_score<70"
    return True, None


def filter_scan(scan: dict) -> tuple[dict, list[str]]:
    filtered = dict(scan)
    excluded: list[str] = []
    excluded_keys: set[tuple[str, str]] = set()

    for signal in ("signal_a", "signal_b", "signal_c", "signal_d", "signal_e", "signal_g"):
        kept = []
        for item in scan.get(signal, []):
            keep, reason = should_keep_signal_item(signal, item)
            if keep:
                kept.append(item)
                continue
            code = str(item.get("code", ""))
            company = str(item.get("company") or item.get("name") or code)
            sig = signal[-1].upper()
            excluded_keys.add((sig, code))
            excluded.append(f"[{sig}] {company}({code}) excluded: {reason}")
        filtered[signal] = kept

    fusion_kept = []
    for item in scan.get("fusion_signals", []):
        code = str(item.get("code", ""))
        signals = [str(sig).upper() for sig in item.get("signals", [])]
        if any((sig, code) in excluded_keys for sig in signals):
            company = str(item.get("company") or code)
            excluded.append(f"[FUSION] {company}({code}) excluded: child signal excluded")
            continue
        fusion_kept.append(item)
    filtered["fusion_signals"] = fusion_kept
    return filtered, excluded


def scan_date_from_payload(scan: dict) -> str:
    raw = str(scan.get("scan_date") or datetime.now().strftime("%Y%m%d"))
    return raw.replace("-", "")


def run_once(args: argparse.Namespace) -> int:
    date_suffix = args.date or datetime.now().strftime("%Y%m%d")
    scan_json = Path(args.scan_json) if args.scan_json else Path(f"/private/tmp/market_distortion_{date_suffix}.json")
    record_json = Path(args.record_json) if args.record_json else Path(f"/private/tmp/market_distortion_{date_suffix}_record.json")

    if args.scan_json:
        print(f"\n## 既存スキャンJSONを使用: {scan_json}")
    else:
        scan_cmd = [
            sys.executable,
            str(SKILL_DIR / "run_scan.py"),
            "--json",
            "--pead-days",
            str(args.pead_days),
        ]
        if args.date:
            scan_cmd.extend(["--date", args.date])
        result = retry_step("歪みスキャンJSON保存", scan_cmd, scan_json)
        if result.returncode != 0:
            return result.returncode

    scan = json.loads(scan_json.read_text(encoding="utf-8"))
    actual_date = scan_date_from_payload(scan)
    if not args.record_json:
        record_json = Path(f"/private/tmp/market_distortion_{actual_date}_record.json")

    filtered, excluded = filter_scan(scan)
    record_json.write_text(json.dumps(filtered, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n## 記録用JSON: {record_json}")
    if excluded:
        print("## 自動除外")
        for line in excluded:
            print("  " + line)
    else:
        print("## 自動除外: なし")

    dry_run_cmd = [
        sys.executable,
        str(SKILL_DIR / "record_signals.py"),
        "--from-json",
        str(record_json),
        "--dry-run",
    ]
    dry_result = retry_step("台帳記録 dry-run", dry_run_cmd)
    if dry_result.returncode != 0:
        return dry_result.returncode

    before_ids = load_tracker_ids()
    if args.no_record:
        print("\n## 台帳記録: --no-record のためスキップ")
    else:
        record_cmd = [
            sys.executable,
            str(SKILL_DIR / "record_signals.py"),
            "--from-json",
            str(record_json),
        ]
        record_result = retry_step("台帳記録", record_cmd)
        if record_result.returncode != 0:
            return record_result.returncode

        after_records = load_tracker_records()
        added = [rec for rec in after_records if int(rec.get("id", 0)) not in before_ids]
        if added:
            print("\n## 追記確認")
            for rec in added:
                print(
                    f"  ID {rec.get('id')}: [{rec.get('signal')}] "
                    f"{rec.get('company')}({rec.get('code')}) {rec.get('date')}"
                )
        else:
            print("\n## 追記確認: 新規記録なし")

        yutai_cmd = [
            sys.executable,
            str(SKILL_DIR.parent / "kasumi-yutai-yield" / "enrich_signal_yutai.py"),
            "--from-json",
            str(record_json),
            "--date",
            actual_date,
        ]
        yutai_result = retry_step("優待情報照合・総合利回り5%以上を優待台帳へ追加", yutai_cmd)
        if yutai_result.returncode != 0:
            return yutai_result.returncode

    pnl_cmd = [
        sys.executable,
        str(SKILL_DIR / "entry_backtest.py"),
        "--entry",
        "next-open",
        "--eval",
        "TODAY",
    ]
    pnl_result = retry_step("損益チェック", pnl_cmd)
    return pnl_result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="日次の歪みチェック・台帳記録・損益チェックを一括実行する")
    parser.add_argument("--date", help="スキャン対象日 YYYYMMDD。未指定なら run_scan.py が直近取引日を判定する")
    parser.add_argument("--pead-days", type=int, default=1, help="PEAD 取得営業日数。日次運用の既定は1")
    parser.add_argument("--scan-json", help="既存のスキャンJSONを使う場合のパス")
    parser.add_argument("--record-json", help="記録用にフィルタ後JSONを書き出すパス")
    parser.add_argument("--no-record", action="store_true", help="dry-run と損益チェックだけ実行し、台帳には書かない")
    parser.add_argument("--loop", action="store_true", help="指定間隔で繰り返し実行する")
    parser.add_argument("--interval-minutes", type=float, default=1440, help="--loop 時の実行間隔。既定は1440分")
    args = parser.parse_args()

    if args.pead_days < 1:
        parser.error("--pead-days must be >= 1")
    if args.interval_minutes <= 0:
        parser.error("--interval-minutes must be > 0")

    if not args.loop:
        return run_once(args)

    exit_code = 0
    while True:
        print(f"\n# daily_flow loop start {datetime.now().isoformat(timespec='seconds')}")
        exit_code = run_once(args)
        print(f"\n# daily_flow loop end code={exit_code}")
        sleep_sec = args.interval_minutes * 60
        print(f"# next run after {args.interval_minutes:g} minutes")
        time.sleep(sleep_sec)


if __name__ == "__main__":
    sys.exit(main())
