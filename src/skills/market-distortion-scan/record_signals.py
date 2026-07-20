#!/usr/bin/env python3
"""
record_signals.py — 歪みスキャン結果をトラッカー台帳に自動記録する

Usage:
    # スキャンを実行して推奨シグナルを台帳記録
    python3 record_signals.py

    # 既存のJSONファイルを読み込んで記録（スキャン省略）
    python3 record_signals.py --from-json path/to/scan.json

    # PEAD の遡り営業日数を指定（日次台帳記録の既定は1日）
    python3 record_signals.py --pead-days 3

    # ドライラン（記録せず確認のみ）
    python3 record_signals.py --dry-run
"""
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

SKILL_DIR = Path(__file__).parent
TRACKER_FILE = SKILL_DIR / 'results' / 'tracker.json'

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# 株価取得（kabutan.jp 前日終値 or 現在値）
# ---------------------------------------------------------------------------

def fetch_kabutan_price(code: str) -> float | None:
    """kabutan.jp から現在値 or 前日終値を取得する"""
    url = f'https://kabutan.jp/stock/?code={code}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            html = r.read().decode('utf-8', errors='ignore')

        # 現在値（市場中）
        m = re.search(r'現在値</th>\s*<td>([\d,]+)', html)
        if not m:
            # 前日終値（市場後・休場日）
            m = re.search(r'前日終値</dt>\s*<dd[^>]*>([\d,]+)', html)
        if m:
            return float(m.group(1).replace(',', ''))
    except Exception as e:
        print(f'  [WARN] {code} 株価取得失敗: {e}', file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# 台帳読み書き
# ---------------------------------------------------------------------------

def load_tracker() -> list[dict]:
    if TRACKER_FILE.exists():
        return json.loads(TRACKER_FILE.read_text(encoding='utf-8'))
    return []


def save_tracker(records: list[dict]) -> None:
    TRACKER_FILE.parent.mkdir(exist_ok=True)
    TRACKER_FILE.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8'
    )


def already_recorded(records: list[dict], code: str, signal: str, scan_date: str) -> bool:
    """同一日・同一銘柄・同一シグナルが既に記録済みか判定"""
    for rec in records:
        if rec['code'] == code and rec['signal'] == signal and rec['date'] == scan_date:
            return True
    return False


# ---------------------------------------------------------------------------
# スキャン結果から記録対象を抽出
# ---------------------------------------------------------------------------

def extract_targets(scan: dict) -> list[dict]:
    """
    スキャン結果から台帳記録対象を抽出する。
    Returns: [{code, company, signal, source}, ...]
    FUSIONは最初のシグナルで代表させ、A-G個別シグナルと重複しないよう管理。
    """
    targets: list[dict] = []
    seen: set[tuple[str, str]] = set()  # (code, signal)

    def add(code, company, signal, source=None):
        key = (code, signal)
        if key not in seen:
            seen.add(key)
            targets.append({
                'code': code,
                'company': company,
                'signal': signal,
                'source': source or {},
            })

    # FUSION（最優先）— 代表シグナルは signals リストの最初
    for item in scan.get('fusion_signals', []):
        sig = item['signals'][0] if item.get('signals') else 'D'
        add(item['code'], item.get('company', item['code']), sig, item)

    # Signal A: 上方修正かつ推奨（recommended=True または quality_score >= 3）
    for item in scan.get('signal_a', []):
        if item.get('recommended') or item.get('quality_score', 0) >= 3:
            add(item['code'], item.get('company', item['code']), 'A', item)

    # Signal B: 大量保有（すべて記録）
    for item in scan.get('signal_b', []):
        add(item['code'], item.get('company', item['code']), 'B', item)

    # Signal C: 自社株買い（推奨のみ）
    for item in scan.get('signal_c', []):
        if item.get('recommended'):
            add(item['code'], item.get('company', item['code']), 'C', item)

    # Signal D: 暴落過剰反応（推奨のみ）
    for item in scan.get('signal_d', []):
        if item.get('recommended'):
            add(item['code'], item.get('company', item['code']), 'D', item)

    # Signal E: PEAD（pead_score >= 70 かつ推奨）
    for item in scan.get('signal_e', []):
        if item.get('recommended') and item.get('pead_score', 0) >= 70:
            add(item['code'], item.get('company', item['code']), 'E', item)

    # Signal G: 信用需給
    for item in scan.get('signal_g', []):
        add(item['code'], item.get('company', item['code']), 'G', item)

    return targets


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def run(from_json: str | None = None, dry_run: bool = False, pead_days: int = 1) -> None:
    # --- スキャン結果取得 ---
    if from_json:
        scan = json.loads(Path(from_json).read_text(encoding='utf-8'))
        print(f'JSONファイルから読み込み: {from_json}')
    else:
        print('歪みスキャン実行中...')
        result = subprocess.run(
            [
                sys.executable,
                str(SKILL_DIR / 'run_scan.py'),
                '--json',
                '--pead-days',
                str(pead_days),
            ],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print('スキャン失敗:', result.stderr[-500:], file=sys.stderr)
            sys.exit(1)
        scan = json.loads(result.stdout)

    scan_date_str = scan.get('scan_date', date.today().strftime('%Y%m%d'))
    scan_date = f'{scan_date_str[:4]}-{scan_date_str[4:6]}-{scan_date_str[6:]}'
    print(f'スキャン日: {scan_date}')

    targets = extract_targets(scan)
    if not targets:
        print('記録対象シグナルがありません。')
        return

    print(f'\n記録対象: {len(targets)}件')

    records = load_tracker()
    new_records: list[dict] = []
    skipped = 0
    next_id = max((int(rec.get('id', 0)) for rec in records), default=0) + 1

    for target in targets:
        code = target['code']
        company = target['company']
        signal = target['signal']
        source = target.get('source') or {}
        # 重複チェック
        if already_recorded(records, code, signal, scan_date):
            print(f'  スキップ（記録済み）: [{signal}] {company}({code})')
            skipped += 1
            continue

        # 株価取得
        price = fetch_kabutan_price(code)
        time.sleep(0.4)  # レート制限対策

        if price is None:
            print(f'  スキップ（株価取得失敗）: [{signal}] {company}({code})')
            skipped += 1
            continue

        print(f'  [{signal}] {company}({code}) @ {price:,.0f}円')

        if not dry_run:
            record = {
                'id': next_id + len(new_records),
                'date': scan_date,
                'event_date': source.get('date'),
                'signal': signal,
                'code': code,
                'company': company,
                'price_at_signal': price,
                'buy_thesis': '',
                'exit_condition': '',
                'returns': {},
            }
            if source.get('title'):
                record['title'] = source.get('title')
            if source.get('reason'):
                record['reason'] = source.get('reason')
            if source.get('tdnet_url'):
                record['tdnet_url'] = source.get('tdnet_url')
            if source.get('financials'):
                record['financials'] = source.get('financials')
            if signal == 'E':
                record['pead_candidate'] = {
                    'pead_score': source.get('pead_score'),
                    'composite_surprise': source.get('composite_surprise'),
                    'text_score': source.get('text_score'),
                    'current_change_pct': source.get('current_change_pct'),
                    'reason': source.get('reason', ''),
                    'tdnet_url': source.get('tdnet_url', ''),
                }
            new_records.append(record)

    if dry_run:
        print(f'\n[DRY RUN] 記録はしません。{len(targets) - skipped}件が記録対象です。')
        return

    if new_records:
        records.extend(new_records)
        save_tracker(records)
        print(f'\n{len(new_records)}件を台帳に記録しました（スキップ: {skipped}件）')
        print(f'  保存先: {TRACKER_FILE}')
    else:
        print(f'\n新規記録なし（スキップ: {skipped}件）')


if __name__ == '__main__':
    from_json = None
    dry_run = False
    pead_days = 1

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--from-json' and i + 1 < len(args):
            from_json = args[i + 1]
            i += 2
        elif args[i] == '--pead-days':
            if i + 1 >= len(args):
                print('ERROR: --pead-days は 1 以上の整数で指定してください', file=sys.stderr)
                sys.exit(2)
            try:
                pead_days = int(args[i + 1])
            except ValueError:
                print('ERROR: --pead-days は 1 以上の整数で指定してください', file=sys.stderr)
                sys.exit(2)
            if pead_days < 1:
                print('ERROR: --pead-days は 1 以上の整数で指定してください', file=sys.stderr)
                sys.exit(2)
            i += 2
        elif args[i] == '--dry-run':
            dry_run = True
            i += 1
        else:
            i += 1

    run(from_json=from_json, dry_run=dry_run, pead_days=pead_days)
