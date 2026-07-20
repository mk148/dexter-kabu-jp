#!/usr/bin/env python3
"""
ledger_deep_dive.py — tracker.json の銘柄を高速に深掘りし、買いテーゼを追記する。

Usage:
    python3 ledger_deep_dive.py --date 2026-05-08 --budget 1000000 --write
    python3 ledger_deep_dive.py --latest --dry-run
    python3 ledger_deep_dive.py --latest --json
"""
import argparse
import html
import json
import re
import ssl
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date
from math import floor
from pathlib import Path

SKILL_DIR = Path(__file__).parent
TRACKER_FILE = SKILL_DIR / 'results' / 'tracker.json'

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


@dataclass
class MarketSnapshot:
    code: str
    close: float | None = None
    close_date: str = ''
    change_pct: float | None = None
    week52_high: float | None = None
    week52_high_date: str = ''
    week52_low: float | None = None
    week52_low_date: str = ''
    drawdown_pct: float | None = None
    per: float | None = None
    pbr: float | None = None
    dividend_yield_pct: float | None = None
    credit_ratio: float | None = None
    market_cap_oku: float | None = None
    error: str = ''


def load_tracker() -> list[dict]:
    if TRACKER_FILE.exists():
        return json.loads(TRACKER_FILE.read_text(encoding='utf-8'))
    return []


def save_tracker(records: list[dict]) -> None:
    TRACKER_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, context=ctx, timeout=12) as r:
        return r.read().decode('utf-8', errors='ignore')


def clean_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = re.sub(r'<.*?>', '', raw)
    text = html.unescape(text).replace(',', '').replace('\xa0', '').strip()
    m = re.search(r'-?\d+(?:\.\d+)?', text)
    return float(m.group(0)) if m else None


def first(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.S)
    return m.group(1) if m else None


def parse_market_cap_oku(raw: str | None) -> float | None:
    if not raw:
        return None
    text = re.sub(r'<.*?>', '', raw)
    text = html.unescape(text).replace(',', '').strip()
    cho = 0.0
    oku = 0.0
    m = re.search(r'(\d+(?:\.\d+)?)\s*兆', text)
    if m:
        cho = float(m.group(1)) * 10000
    m = re.search(r'兆\s*(\d+(?:\.\d+)?)\s*億円', text)
    if m:
        oku = float(m.group(1))
    elif not cho:
        m = re.search(r'(\d+(?:\.\d+)?)\s*億円', text)
        if m:
            oku = float(m.group(1))
    return cho + oku if cho or oku else None


def fetch_snapshot(code: str) -> MarketSnapshot:
    snap = MarketSnapshot(code=code)
    try:
        main = fetch(f'https://kabutan.jp/stock/?code={code}')
        hist = fetch(f'https://s.kabutan.jp/stocks/{code}/historical_prices/daily/')

        snap.close = clean_number(first(r"<th scope='row'>終値</th>\s*<td>([^<]+)</td>", main))
        snap.close_date = first(r'<h2><time datetime="([^"]+)">', main) or ''
        snap.change_pct = clean_number(first(
            r"<td><span class='[^']*?num'>([+-]?[\d,.]+)</span><span class='pl-px text-10px font-light [^']*?num'>%",
            hist,
        ))

        start = hist.find('52週高値')
        end = hist.find('年初来高値')
        block = hist[start:end if end != -1 else start + 1200] if start != -1 else ''
        values = re.findall(
            r'<span class="text-lg">([\d,]+(?:\.\d+)?)</span>\s*<span>\(([^)]+)\)</span>',
            block,
            re.S,
        )
        if len(values) >= 1:
            snap.week52_high = clean_number(values[0][0])
            snap.week52_high_date = values[0][1]
        if len(values) >= 2:
            snap.week52_low = clean_number(values[1][0])
            snap.week52_low_date = values[1][1]
        if snap.close and snap.week52_high:
            snap.drawdown_pct = round((snap.close - snap.week52_high) / snap.week52_high * 100, 1)

        row = first(r'data-help="PER".*?</thead>\s*<tbody>\s*<tr>(.*?)</tr>', main) or ''
        tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
        snap.per = clean_number(tds[0]) if len(tds) > 0 else None
        snap.pbr = clean_number(tds[1]) if len(tds) > 1 else None
        snap.dividend_yield_pct = clean_number(tds[2]) if len(tds) > 2 else None
        snap.credit_ratio = clean_number(tds[3]) if len(tds) > 3 else None
        snap.market_cap_oku = parse_market_cap_oku(first(r'時価総額</th>\s*<td[^>]*>(.*?)</td>', main))
    except Exception as e:
        snap.error = str(e)
    return snap


def latest_tracker_date(records: list[dict]) -> str:
    dates = [rec.get('date', '') for rec in records if rec.get('date')]
    return max(dates) if dates else date.today().isoformat()


def choose_records(records: list[dict], target_date: str | None, latest: bool) -> list[dict]:
    if latest:
        target_date = latest_tracker_date(records)
    if target_date:
        return [rec for rec in records if rec.get('date') == target_date]
    return records


def yen(value: float | None) -> str:
    return 'NA' if value is None else f'{value:,.0f}円'


def pct(value: float | None) -> str:
    return 'NA' if value is None else f'{value:.1f}%'


def compute_score(rec: dict, snap: MarketSnapshot) -> float:
    score = 0.0
    if snap.drawdown_pct is not None:
        score += min(abs(snap.drawdown_pct), 50) * 1.2
    if snap.per is not None:
        if snap.per <= 10:
            score += 18
        elif snap.per <= 15:
            score += 12
        elif snap.per <= 20:
            score += 6
        elif snap.per >= 30:
            score -= 8
    if snap.pbr is not None:
        if snap.pbr < 0.5:
            score += 16
        elif snap.pbr < 1.0:
            score += 10
        elif snap.pbr > 3.0:
            score -= 8
    if snap.dividend_yield_pct is not None:
        score += min(snap.dividend_yield_pct, 5) * 2
    signal = rec.get('signal')
    if signal == 'D':
        score += 8
    elif signal == 'C':
        score += 6
    elif signal == 'E':
        score += 4
    if snap.market_cap_oku is not None and snap.market_cap_oku <= 100:
        score += 6
    if snap.error:
        score -= 20
    return round(score, 1)


def classify(score: float, snap: MarketSnapshot) -> str:
    if score >= 65:
        return '本命'
    if score >= 55:
        return '攻め'
    if score >= 45:
        return '上位候補'
    if score >= 35:
        return '中位候補'
    return '見送り寄り'


def build_thesis(rec: dict, snap: MarketSnapshot, rank: int, stance: str) -> str:
    company = rec.get('company') or rec.get('code')
    parts = [f'{stance}。比較順位{rank}位。']
    if snap.week52_high:
        parts.append(f'52週高値{yen(snap.week52_high)}から{pct(abs(snap.drawdown_pct))}下落。')
    if snap.per is not None or snap.pbr is not None or snap.dividend_yield_pct is not None:
        parts.append(
            f'PER {snap.per if snap.per is not None else "NA"}倍、'
            f'PBR {snap.pbr if snap.pbr is not None else "NA"}倍、'
            f'配当利回り{pct(snap.dividend_yield_pct)}。'
        )
    signal = rec.get('signal')
    if signal == 'D':
        parts.append('暴落過剰反応シグナルのため、短期反発余地と追加悪材料の有無を重視する。')
    elif signal == 'C':
        parts.append('自社株買いシグナルのため、PBR修正と株主還元効果を重視する。')
    elif signal == 'E':
        parts.append('PEADシグナルのため、決算サプライズ後のドリフト継続を重視する。')
    if stance == '本命':
        parts.append(f'{company}は下落率・バリュエーション・シグナルの総合点が最も高い。')
    elif stance == '攻め':
        parts.append('戻り余地は大きいが、値動きの荒さを前提に短期で管理する。')
    elif stance == '見送り寄り':
        parts.append('現時点では価格妙味または財務品質に弱さがあり、深追いは避ける。')
    return ''.join(parts)


def build_exit_condition(snap: MarketSnapshot, stance: str) -> str:
    if snap.close is None:
        return '株価取得失敗のため、再取得後にT5/T10/T20で見直し。'
    if stance in {'本命', '攻め'}:
        target = round(snap.close * 1.15)
        stop = round(snap.close * 0.90)
        return f'{target:,.0f}円近辺への反発、またはT20到達で見直し。{stop:,.0f}円割れ定着、出来高減少、追加悪材料なら撤退検討。'
    if stance == '上位候補':
        target = round(snap.close * 1.10)
        return f'{target:,.0f}円近辺への戻り、またはT20到達で見直し。シグナル後の反応が弱い場合は資金効率を再評価。'
    return 'T5/T10で反発を確認。反応が弱い、または追加材料が出ない場合は見送り継続。'


def enrich(records: list[dict], budget: float, max_workers: int) -> list[dict]:
    snapshots: dict[str, MarketSnapshot] = {}
    codes = sorted({str(rec['code']) for rec in records})
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_snapshot, code): code for code in codes}
        for future in as_completed(futures):
            snap = future.result()
            snapshots[snap.code] = snap

    rows: list[dict] = []
    for rec in records:
        snap = snapshots.get(str(rec['code']), MarketSnapshot(code=str(rec['code']), error='not fetched'))
        close = snap.close or rec.get('price_at_signal')
        lots = floor(budget / (close * 100)) if close else 0
        score = compute_score(rec, snap)
        rows.append({
            'record': rec,
            'snapshot': snap,
            'score': score,
            'lots': lots,
            'shares': lots * 100,
            'amount': round((lots * 100) * close, 1) if close else 0,
        })

    rows.sort(key=lambda row: row['score'], reverse=True)
    for idx, row in enumerate(rows, start=1):
        stance = classify(row['score'], row['snapshot'])
        row['rank'] = idx
        row['stance'] = stance
        row['buy_thesis'] = build_thesis(row['record'], row['snapshot'], idx, stance)
        row['exit_condition'] = build_exit_condition(row['snapshot'], stance)
    return rows


def apply_rows(all_records: list[dict], rows: list[dict], budget: float) -> None:
    by_id = {row['record']['id']: row for row in rows}
    for rec in all_records:
        row = by_id.get(rec.get('id'))
        if not row:
            continue
        snap = row['snapshot']
        rec['buy_thesis'] = row['buy_thesis']
        rec['exit_condition'] = row['exit_condition']
        rec['deep_dive'] = {
            'as_of': snap.close_date,
            'budget_yen': budget,
            'rank': row['rank'],
            'stance': row['stance'],
            'score': row['score'],
            'close': snap.close,
            'change_pct': snap.change_pct,
            'week52_high': snap.week52_high,
            'week52_high_date': snap.week52_high_date,
            'week52_low': snap.week52_low,
            'week52_low_date': snap.week52_low_date,
            'drawdown_pct': snap.drawdown_pct,
            'per': snap.per,
            'pbr': snap.pbr,
            'dividend_yield_pct': snap.dividend_yield_pct,
            'credit_ratio': snap.credit_ratio,
            'market_cap_oku': snap.market_cap_oku,
            'max_lots_100': row['lots'],
            'max_shares': row['shares'],
            'max_amount_yen': row['amount'],
        }


def print_table(rows: list[dict]) -> None:
    print('rank code company signal close drawdown PER PBR yield shares amount stance score')
    for row in rows:
        rec = row['record']
        snap = row['snapshot']
        print(
            f"{row['rank']:>2} "
            f"{rec.get('code')} "
            f"{rec.get('company')} "
            f"{rec.get('signal')} "
            f"{yen(snap.close):>10} "
            f"{pct(snap.drawdown_pct):>7} "
            f"{snap.per if snap.per is not None else 'NA':>5} "
            f"{snap.pbr if snap.pbr is not None else 'NA':>5} "
            f"{pct(snap.dividend_yield_pct):>6} "
            f"{row['shares']:>6} "
            f"{yen(row['amount']):>10} "
            f"{row['stance']} "
            f"{row['score']}"
        )
        if snap.error:
            print(f"    WARN: {snap.error}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', help='対象台帳日付 YYYY-MM-DD')
    parser.add_argument('--latest', action='store_true', help='台帳の最新日付を対象にする')
    parser.add_argument('--budget', type=float, default=1_000_000, help='1銘柄あたり購入上限額')
    parser.add_argument('--write', action='store_true', help='tracker.json に追記する')
    parser.add_argument('--dry-run', action='store_true', help='追記せず比較表だけ表示する')
    parser.add_argument('--json', action='store_true', help='比較結果をJSONで出力する')
    parser.add_argument('--max-workers', type=int, default=6, help='株探取得の並列数')
    args = parser.parse_args()

    all_records = load_tracker()
    target_records = choose_records(all_records, args.date, args.latest or not args.date)
    if not target_records:
        print('対象レコードがありません', file=sys.stderr)
        sys.exit(1)

    rows = enrich(target_records, args.budget, max(1, args.max_workers))
    if args.json:
        payload = []
        for row in rows:
            item = {
                'rank': row['rank'],
                'stance': row['stance'],
                'score': row['score'],
                'record': row['record'],
                'snapshot': asdict(row['snapshot']),
                'max_shares': row['shares'],
                'max_amount_yen': row['amount'],
                'buy_thesis': row['buy_thesis'],
                'exit_condition': row['exit_condition'],
            }
            payload.append(item)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_table(rows)

    if args.write and not args.dry_run:
        apply_rows(all_records, rows, args.budget)
        save_tracker(all_records)
        print(f'\nupdated: {TRACKER_FILE}')
    elif args.dry_run:
        print('\ndry-run: tracker.json は更新していません')


if __name__ == '__main__':
    main()
