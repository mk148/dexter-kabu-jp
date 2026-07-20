#!/usr/bin/env python3
"""
validate.py — 過去TDNetデータで各シグナルのバックテスト統計検証

Usage:
    python3 src/skills/market-distortion-scan/validate.py
    python3 src/skills/market-distortion-scan/validate.py --signal B
    python3 src/skills/market-distortion-scan/validate.py --signal B --from 2024-01-01 --to 2025-12-31
    python3 src/skills/market-distortion-scan/validate.py --signal C --from 2024-04-01 --to 2025-06-30
    python3 src/skills/market-distortion-scan/validate.py --all --from 2024-01-01 --to 2025-12-31

Notes:
    - TDNetデータはキャッシュ（results/validate_cache/tdnet/）に保存
    - JQuants株価もキャッシュ（results/validate_cache/prices/）に保存
    - JQuants Freeプランは12週遅延 → 2024-01〜2025-12のデータは2026-05時点で取得可能
    - t検定は純粋Python実装（scipy不要）、n>=30 で正規分布近似
    - Signal D（急落）は歴史的ランキングが取得不可のため注意書きのみ

Supported signals for historical backtest:
    B  — 大量保有報告書（新規・変更）
    C  — 自己株式取得（取得授権・実施報告）
    E  — PEAD: 決算短信サプライズ（backtest.py ledger から読込）
"""
import argparse
import json
import math
import re
import ssl
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

SKILL_DIR = Path(__file__).parent
CACHE_DIR = SKILL_DIR / 'results' / 'validate_cache'
TDNET_CACHE_DIR = CACHE_DIR / 'tdnet'
PRICE_CACHE_DIR = CACHE_DIR / 'prices'
BACKTEST_LEDGER = SKILL_DIR / 'results' / 'backtest' / 'signals.json'
ENV_FILE = SKILL_DIR.parent.parent.parent / '.env'

CHECKPOINTS = {'T5': 5, 'T10': 10, 'T20': 20}
TOPIX_ETF_CODE = '1306'   # NEXT FUNDS TOPIX ETF

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}


# ── 環境変数 ──────────────────────────────────────────────────────────────────

def load_api_key() -> str:
    if not ENV_FILE.exists():
        return ''
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        if line.startswith('JQUANTS_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"\'')
    return ''


# ── 統計関数（scipy不要） ──────────────────────────────────────────────────────

def _mean(data: list[float]) -> float:
    return sum(data) / len(data)


def _std(data: list[float]) -> float:
    m = _mean(data)
    return math.sqrt(sum((x - m) ** 2 for x in data) / (len(data) - 1))


def _median(data: list[float]) -> float:
    s = sorted(data)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def ttest_1samp(data: list[float], popmean: float = 0.0) -> tuple[float, float]:
    """1標本t検定。(t_stat, p_value) を返す。n>=30 で正規分布近似。"""
    n = len(data)
    if n < 2:
        return float('nan'), float('nan')
    m = _mean(data)
    s = _std(data)
    if s == 0:
        return float('nan'), float('nan')
    t = (m - popmean) / (s / math.sqrt(n))
    # 両側p値: 正規分布近似（n>=30で有効）
    z = abs(t)
    p = 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))
    return t, p


def sharpe_ratio(returns_pct: list[float], n_days: int, risk_free_annual: float = 0.001) -> float:
    """保有期間ベースのSharpe比（年率換算）。returns_pct は % 単位。"""
    if len(returns_pct) < 2:
        return float('nan')
    rf_period = risk_free_annual * n_days / 252
    excess = [r / 100 - rf_period for r in returns_pct]
    m = _mean(excess)
    s = _std(excess)
    if s == 0:
        return float('nan')
    return m / s * math.sqrt(252 / n_days)


def compute_stats(returns: list[float], n_days: int) -> dict:
    """統計指標一式を返す。"""
    if not returns:
        return {'n': 0}
    wins = [r for r in returns if r > 0]
    t_stat, p_val = ttest_1samp(returns)
    sr = sharpe_ratio(returns, n_days)
    return {
        'n': len(returns),
        'win_rate': len(wins) / len(returns) * 100,
        'mean': _mean(returns),
        'median': _median(returns),
        'std': _std(returns) if len(returns) >= 2 else float('nan'),
        'min': min(returns),
        'max': max(returns),
        't_stat': round(t_stat, 3) if not math.isnan(t_stat) else None,
        'p_value': round(p_val, 4) if not math.isnan(p_val) else None,
        'sharpe': round(sr, 3) if not math.isnan(sr) else None,
        'significant': (p_val < 0.05) if not math.isnan(p_val) else None,
    }


# ── ネットワーク・キャッシュ ──────────────────────────────────────────────────

def http_get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='replace')


# urllib を遅延インポートせずに使えるよう
import urllib.request  # noqa: E402 (after function defs for readability)


def load_tdnet_cache(date_str: str) -> list[dict] | None:
    path = TDNET_CACHE_DIR / f'{date_str}.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    return None


def save_tdnet_cache(date_str: str, items: list[dict]) -> None:
    TDNET_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = TDNET_CACHE_DIR / f'{date_str}.json'
    path.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')


def load_price_cache(code4: str) -> dict[str, float]:
    path = PRICE_CACHE_DIR / f'{code4}.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    return {}


def save_price_cache(code4: str, prices: dict[str, float]) -> None:
    PRICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = PRICE_CACHE_DIR / f'{code4}.json'
    path.write_text(json.dumps(prices, ensure_ascii=False), encoding='utf-8')


# ── TDNetスクレイピング ────────────────────────────────────────────────────────

def fetch_tdnet_day(date_str: str, verbose: bool = False) -> list[dict]:
    """指定日のTDNet開示を全ページ取得。キャッシュがあれば返す。"""
    cached = load_tdnet_cache(date_str)
    if cached is not None:
        return cached

    items = []
    for page in range(1, 20):
        url = f'https://www.release.tdnet.info/inbs/I_list_{page:03d}_{date_str}.html'
        try:
            html = http_get(url)
        except Exception:
            break

        if '開示された情報はありません' in html:
            break

        for row in re.findall(r'<tr>(.*?)</tr>', html, re.DOTALL):
            time_m = re.search(r'class="[^"]*kjTime[^"]*"[^>]*>(\d{2}:\d{2})', row)
            code_m = re.search(r'class="[^"]*kjCode[^"]*"[^>]*>\s*(\d{4,5})\s*', row)
            name_m = re.search(r'class="[^"]*kjName[^"]*"[^>]*>(.*?)</td>', row, re.DOTALL)
            title_m = re.search(r'class="[^"]*kjTitle[^"]*".*?<a[^>]+>([^<]+)</a>', row, re.DOTALL)
            if not (code_m and title_m):
                continue
            items.append({
                'code': code_m.group(1)[:4],
                'company': re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', name_m.group(1))).strip() if name_m else '',
                'title': title_m.group(1).strip(),
                'time': time_m.group(1) if time_m else '',
                'date': date_str,
            })

        total_m = re.search(r'全(\d+)件', html)
        if total_m and page * 100 >= int(total_m.group(1)):
            break
        time.sleep(0.3)

    if verbose:
        print(f'  {date_str}: {len(items)}件取得', flush=True)

    save_tdnet_cache(date_str, items)
    return items


# ── シグナル分類 ───────────────────────────────────────────────────────────────

def classify_b(item: dict) -> bool:
    """Signal B: 大量保有報告書（新規・変更）"""
    t = item.get('title', '')
    return '大量保有報告書' in t or ('大量保有' in t and '変更報告書' in t)


def classify_c(item: dict) -> bool:
    """Signal C: 自己株式取得（取得決議・実施状況）"""
    t = item.get('title', '')
    BUYBACK_PATTERNS = [
        '自己株式取得', '自己株式の取得', '自己株式の買付け',
        '自己株買い', '自己株の取得', '自己株取得状況',
    ]
    return any(p in t for p in BUYBACK_PATTERNS)


SIGNAL_CLASSIFIERS = {
    'B': classify_b,
    'C': classify_c,
}


def extract_signals(items: list[dict], signal: str) -> list[dict]:
    """TDNet開示リストから指定シグナルの候補を抽出。"""
    classifier = SIGNAL_CLASSIFIERS.get(signal)
    if not classifier:
        return []
    results = []
    seen = set()
    for item in items:
        if classifier(item):
            code = item['code']
            if code in seen:
                continue
            seen.add(code)
            results.append({
                'signal': signal,
                'code': code,
                'company': item.get('company', ''),
                'title': item.get('title', ''),
                'time': item.get('time', ''),
                'event_date': f"{item['date'][:4]}-{item['date'][4:6]}-{item['date'][6:]}",
            })
    return results


# ── JQuants株価取得 ───────────────────────────────────────────────────────────

def fetch_jquants(code4: str, from_date: str, to_date: str, api_key: str) -> dict[str, float]:
    """JQuants v2 API で調整済終値を取得。キャッシュ込み。"""
    cached = load_price_cache(code4)
    # キャッシュに要求期間が含まれているか確認
    needed_dates = _date_range(from_date, to_date)
    if all(d in cached for d in needed_dates if _is_weekday(d)):
        # 全営業日がキャッシュにある（実際は取引日のみキャッシュされるので近似チェック）
        pass  # キャッシュ使用

    code5 = code4 + '0'
    url = f'https://api.jquants.com/v2/equities/bars/daily?code={code5}&from={from_date}&to={to_date}'
    req = urllib.request.Request(url, headers={'x-api-key': api_key, **HEADERS})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            payload = json.loads(r.read())
    except Exception:
        return cached

    new_prices: dict[str, float] = {}
    for row in payload.get('data', []):
        d = row.get('Date')
        close = row.get('AdjC')
        if d and close is not None:
            new_prices[d] = float(close)

    if new_prices:
        cached.update(new_prices)
        save_price_cache(code4, cached)

    return cached


def _date_range(from_date: str, to_date: str) -> list[str]:
    d = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    result = []
    while d <= end:
        result.append(d.isoformat())
        d += timedelta(days=1)
    return result


def _is_weekday(date_str: str) -> bool:
    return date.fromisoformat(date_str).weekday() < 5


def nth_trading_day(from_str: str, n: int) -> str:
    d = date.fromisoformat(from_str)
    count = 0
    while count < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return d.isoformat()


def next_trading_day(from_str: str) -> str:
    return nth_trading_day(from_str, 1)


def price_on_or_near(prices: dict[str, float], target: str, window: int = 3) -> float | None:
    """target日に最も近い取引日の価格を返す。"""
    td = date.fromisoformat(target)
    for delta in range(0, window + 1):
        for sign in ([0] if delta == 0 else [1, -1]):
            d = (td + timedelta(days=delta * sign)).isoformat()
            if d in prices:
                return prices[d]
    return None


# ── トレーディング日リスト生成 ────────────────────────────────────────────────

def trading_days(from_date: str, to_date: str) -> list[str]:
    d = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    result = []
    while d <= end:
        if d.weekday() < 5:
            result.append(d.isoformat())
        d += timedelta(days=1)
    return result


# ── メインバックテスト ─────────────────────────────────────────────────────────

def run_backtest(
    signal: str,
    from_date: str,
    to_date: str,
    api_key: str,
    verbose: bool = False,
) -> list[dict]:
    """指定シグナルの全イベントを収集し、T+5/10/20リターンを計算して返す。"""
    if signal not in SIGNAL_CLASSIFIERS:
        print(f'ERROR: --signal {signal} はvalidate.pyでは未対応。backtest.pyを参照してください。')
        sys.exit(1)

    days = trading_days(from_date, to_date)
    print(f'[{signal}] {from_date}〜{to_date} TDNetスキャン: {len(days)}営業日', flush=True)

    events: list[dict] = []
    for i, day in enumerate(days):
        date_str = day.replace('-', '')
        tdnet_items = fetch_tdnet_day(date_str, verbose=verbose)
        signals = extract_signals(tdnet_items, signal)
        events.extend(signals)
        if (i + 1) % 20 == 0:
            print(f'  進捗: {i+1}/{len(days)}日, シグナル累計={len(events)}件', flush=True)
        if not verbose:
            time.sleep(0.1)  # キャッシュなしの場合に備えて

    print(f'[{signal}] イベント件数: {len(events)}件 → 価格取得中...', flush=True)

    records: list[dict] = []
    price_cache_by_code: dict[str, dict[str, float]] = {}

    for ev in events:
        code = ev['code']
        event_date = ev['event_date']

        # エントリー日 = 翌営業日
        entry_date = next_trading_day(event_date)
        # T+20終値を取得するための期間
        t20_date = nth_trading_day(event_date, 22)  # 少し余裕を持つ

        if code not in price_cache_by_code:
            prices = fetch_jquants(code, entry_date, t20_date, api_key)
            price_cache_by_code[code] = prices
        else:
            prices = price_cache_by_code[code]
            if t20_date not in prices:
                new_prices = fetch_jquants(code, entry_date, t20_date, api_key)
                price_cache_by_code[code] = new_prices
                prices = new_prices

        entry_price = price_on_or_near(prices, entry_date)
        if entry_price is None:
            continue

        rec = {
            **ev,
            'entry_date': entry_date,
            'entry_price': entry_price,
            'returns': {},
        }

        for label, n in CHECKPOINTS.items():
            target = nth_trading_day(event_date, n)
            p = price_on_or_near(prices, target)
            if p is not None:
                rec['returns'][label] = round((p - entry_price) / entry_price * 100, 2)

        records.append(rec)

    return records


def fetch_topix_returns(from_date: str, to_date: str, api_key: str) -> dict[str, list[float]]:
    """TOPIX ETF(1306) のT+5/10/20リターン分布（ランダムエントリー）を計算。"""
    prices = fetch_jquants(TOPIX_ETF_CODE, from_date, to_date, api_key)
    if not prices:
        return {}

    days = sorted(prices.keys())
    result: dict[str, list[float]] = defaultdict(list)

    for day in days:
        for label, n in CHECKPOINTS.items():
            target = nth_trading_day(day, n)
            p = price_on_or_near(prices, target, window=2)
            if p is not None:
                result[label].append(round((p - prices[day]) / prices[day] * 100, 2))

    return dict(result)


# ── E signal: backtest.pyのledgerから読込 ────────────────────────────────────

def load_e_from_ledger(from_date: str, to_date: str) -> list[dict]:
    """backtest.py の signals.json から Signal E レコードを読み込む。"""
    if not BACKTEST_LEDGER.exists():
        print(f'WARN: backtest ledger が見つかりません: {BACKTEST_LEDGER}', file=sys.stderr)
        return []
    records = json.loads(BACKTEST_LEDGER.read_text(encoding='utf-8'))
    result = []
    for rec in records:
        if rec.get('signal') != 'E':
            continue
        event_date = rec.get('event_date', '')
        if not (from_date <= event_date <= to_date):
            continue
        result.append({
            'signal': 'E',
            'code': rec['code'],
            'company': rec.get('company', ''),
            'event_date': event_date,
            'entry_date': rec.get('entry_date', ''),
            'entry_price': rec.get('entry_price'),
            'returns': rec.get('returns', {}),
        })
    return result


# ── レポート出力 ───────────────────────────────────────────────────────────────

def print_stats_table(signal: str, records: list[dict], topix_returns: dict[str, list[float]]) -> None:
    """統計テーブルを標準出力に印字。"""
    print(f'\n{"="*72}')
    print(f'Signal {signal} バックテスト結果 (n={len(records)}件)')
    print(f'{"="*72}')
    print(f'{"期間":<8} {"n":>5} {"勝率":>7} {"平均%":>8} {"中央値%":>8} {"SD":>7} '
          f'{"t値":>7} {"p値":>7} {"Sharpe":>8} {"有意":>5}')
    print('-' * 72)

    for label, n_days in CHECKPOINTS.items():
        ret = []
        for rec in records:
            rv = rec.get('returns', {})
            if isinstance(rv, dict):
                v = rv.get(label)
                if v is not None:
                    # backtest.pyの形式: {T5: {return_pct: ...}} or {T5: float}
                    if isinstance(v, dict):
                        v = v.get('return_pct')
                    if v is not None:
                        ret.append(float(v))

        s = compute_stats(ret, n_days)
        if s['n'] == 0:
            print(f'{label:<8} {"0":>5} {"—":>7} {"—":>8} {"—":>8} {"—":>7} {"—":>7} {"—":>7} {"—":>8} {"—":>5}')
            continue

        # TOPIX比較
        topix = topix_returns.get(label, [])
        if topix:
            topix_mean = _mean(topix)
            alpha = s['mean'] - topix_mean
            alpha_str = f'α={alpha:+.2f}%'
        else:
            alpha_str = ''

        sig_mark = '★' if s.get('significant') else ' '
        print(
            f'{label:<8} {s["n"]:>5} {s["win_rate"]:>6.1f}% '
            f'{s["mean"]:>+7.2f}% {s["median"]:>+7.2f}% {s["std"]:>6.2f} '
            f'{s["t_stat"] or "—":>7} {s["p_value"] or "—":>7} '
            f'{s["sharpe"] or "—":>8} {sig_mark:>5}'
        )
        if alpha_str:
            print(f'         vs TOPIX: {alpha_str}')

    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='市場歪みスキャン シグナル有効性バックテスト',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--signal', choices=['B', 'C', 'E', 'all'], default='all',
                        help='対象シグナル (default: all)')
    parser.add_argument('--from', dest='from_date', default='2024-01-01',
                        help='開始日 YYYY-MM-DD (default: 2024-01-01)')
    parser.add_argument('--to', dest='to_date', default='2025-12-31',
                        help='終了日 YYYY-MM-DD (default: 2025-12-31)')
    parser.add_argument('--verbose', action='store_true', help='詳細ログを出力')
    parser.add_argument('--no-topix', action='store_true', help='TOPIX比較をスキップ')
    parser.add_argument('--json', action='store_true', help='JSONで統計出力')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = load_api_key()
    if not api_key:
        print('ERROR: JQUANTS_API_KEY が .env に設定されていません', file=sys.stderr)
        sys.exit(1)

    targets = ['B', 'C', 'E'] if args.signal == 'all' else [args.signal]

    # TOPIX ベンチマーク
    topix_returns: dict[str, list[float]] = {}
    if not args.no_topix:
        print('TOPIX ETF(1306) ベンチマーク取得中...', flush=True)
        topix_returns = fetch_topix_returns(args.from_date, args.to_date, api_key)
        if topix_returns:
            for label, vals in topix_returns.items():
                if vals:
                    print(f'  TOPIX {label}: n={len(vals)}, 平均={_mean(vals):+.2f}%')
        print()

    all_results: dict[str, list[dict]] = {}

    for sig in targets:
        if sig == 'E':
            records = load_e_from_ledger(args.from_date, args.to_date)
            print(f'[E] backtest ledger から {len(records)}件読み込み')
        else:
            records = run_backtest(sig, args.from_date, args.to_date, api_key, verbose=args.verbose)

        all_results[sig] = records

        if args.json:
            continue
        print_stats_table(sig, records, topix_returns)

    if args.json:
        output: dict = {}
        for sig, records in all_results.items():
            sig_stats: dict = {'n_events': len(records)}
            for label, n_days in CHECKPOINTS.items():
                ret = []
                for rec in records:
                    rv = rec.get('returns', {})
                    if isinstance(rv, dict):
                        v = rv.get(label)
                        if v is not None:
                            if isinstance(v, dict):
                                v = v.get('return_pct')
                            if v is not None:
                                ret.append(float(v))
                sig_stats[label] = compute_stats(ret, n_days)
            output[sig] = sig_stats
        print(json.dumps(output, ensure_ascii=False, indent=2))

    print('\n注意事項:')
    print('  - Signal D (急落反転) は歴史的ランキングデータが取得不可のためbacktest.pyを使用してください')
    print('  - Signal E は backtest.py のcollect/updateで蓄積されたデータに依存します')
    print(f'  - キャッシュ: {CACHE_DIR}')


if __name__ == '__main__':
    main()
