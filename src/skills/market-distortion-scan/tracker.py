#!/usr/bin/env python3
"""
market-distortion-scan / tracker.py
シグナル発生日・銘柄・価格を記録し、T+5/T+10/T+20 の損益を自動計算する。

Usage:
    # シグナルを記録
    python3 tracker.py record --signal A --code 7203 --price 2500 [--company トヨタ]

    # 損益チェック（JQuants で現在価格取得）
    python3 tracker.py check

    # 統計サマリー
    python3 tracker.py stats

    # 全記録表示
    python3 tracker.py list
"""
import json
import math
import re
import ssl
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

SKILL_DIR = Path(__file__).parent
TRACKER_FILE = SKILL_DIR / 'results' / 'tracker.json'
ENV_FILE = Path(__file__).parent.parent.parent.parent / '.env'

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def load_env_key() -> str:
    """Load JQUANTS_API_KEY from .env"""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith('JQUANTS_API_KEY='):
                return line.split('=', 1)[1].strip().strip('"\'')
    return ''


def load_tracker() -> list[dict]:
    if TRACKER_FILE.exists():
        return json.loads(TRACKER_FILE.read_text(encoding='utf-8'))
    return []


def save_tracker(records: list[dict]) -> None:
    TRACKER_FILE.parent.mkdir(exist_ok=True)
    TRACKER_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')


def fetch_price(code4: str, from_date: str, to_date: str, api_key: str) -> dict[str, float]:
    """kabutan.jp から日付→終値 の辞書を返す（JQuants Freeプラン非対応のため代替）"""
    result: dict[str, float] = {}
    from_d = date.fromisoformat(from_date)
    to_d = date.fromisoformat(to_date)

    for page in range(1, 6):  # 最大5ページ（約150営業日分）
        url = f'https://kabutan.jp/stock/kabuka?code={code4}&ashi=day&page={page}'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                html = r.read().decode('utf-8')
        except Exception:
            break

        # <time datetime="YYYY-MM-DD"> の次の4つの<td>が 始値/高値/安値/終値
        rows = re.findall(
            r'<time datetime="(\d{4}-\d{2}-\d{2})">[^<]*</time></th>'
            r'(?:.*?<td[^>]*>){4}([\d,]+)',
            html, re.S
        )
        if not rows:
            break

        page_oldest = None
        for dt_str, close_str in rows:
            d = date.fromisoformat(dt_str)
            page_oldest = d
            if from_d <= d <= to_d:
                result[dt_str] = float(close_str.replace(',', ''))

        # ページの最古日が from_date より古ければ終了
        if page_oldest and page_oldest < from_d:
            break

    return result


def nth_trading_day(from_date_str: str, n: int) -> str:
    """from_date から n 営業日後の日付を返す（簡易: 土日スキップのみ）"""
    d = date.fromisoformat(from_date_str)
    count = 0
    while count < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return d.isoformat()


def cmd_record(args: list[str]) -> None:
    signal = code = price = company = None
    buy_thesis = ''
    exit_cond = ''
    i = 0
    while i < len(args):
        if args[i] == '--signal' and i + 1 < len(args):
            signal = args[i + 1]; i += 2
        elif args[i] == '--code' and i + 1 < len(args):
            code = args[i + 1]; i += 2
        elif args[i] == '--price' and i + 1 < len(args):
            price = float(args[i + 1]); i += 2
        elif args[i] == '--company' and i + 1 < len(args):
            company = args[i + 1]; i += 2
        elif args[i] == '--thesis' and i + 1 < len(args):
            buy_thesis = args[i + 1]; i += 2
        elif args[i] == '--exit' and i + 1 < len(args):
            exit_cond = args[i + 1]; i += 2
        else:
            i += 1

    if not all([signal, code, price]):
        print('Usage: tracker.py record --signal A --code 7203 --price 2500 [--company 銘柄名]')
        sys.exit(1)

    records = load_tracker()
    record = {
        'id': len(records) + 1,
        'date': date.today().isoformat(),
        'signal': signal,
        'code': code,
        'company': company or code,
        'price_at_signal': price,
        'buy_thesis': buy_thesis,
        'exit_condition': exit_cond,
        'returns': {},  # {T5: pct, T10: pct, T20: pct}
    }
    records.append(record)
    save_tracker(records)
    print(f'記録しました: [{signal}] {company or code}({code}) @ {price:,.0f}円 ({date.today()})')


def cmd_check(args: list[str]) -> None:
    api_key = load_env_key()  # kabutan.jpスクレイピングのため不要だが互換性のため残す

    records = load_tracker()
    if not records:
        print('記録がありません。tracker.py record で記録してください。')
        return

    today = date.today().isoformat()
    updated = False

    for rec in records:
        signal_date = rec['date']
        code = rec['code']
        entry_price = rec['price_at_signal']
        returns = rec.get('returns', {})

        checkpoints = {'T5': 5, 'T10': 10, 'T20': 20}
        for label, n in checkpoints.items():
            if label in returns:
                continue  # 計算済み
            target_date = nth_trading_day(signal_date, n)
            if target_date > today:
                continue  # まだ到達していない
            prices = fetch_price(code, target_date, target_date, api_key)
            if not prices:
                # 前後1日探す
                for delta in [1, -1, 2, -2]:
                    d = (date.fromisoformat(target_date) + timedelta(days=delta)).isoformat()
                    prices = fetch_price(code, d, d, api_key)
                    if prices:
                        break
            if prices:
                close_price = list(prices.values())[0]
                pct = (close_price - entry_price) / entry_price * 100
                returns[label] = round(pct, 2)
                updated = True

        rec['returns'] = returns

    if updated:
        save_tracker(records)
        print('損益を更新しました。')

    # 表示
    print('\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    print('シグナル追跡結果')
    print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    for rec in records[-20:]:  # 直近20件
        r = rec.get('returns', {})
        t5 = f"{r['T5']:+.1f}%" if 'T5' in r else 'T5:未到達'
        t10 = f"{r['T10']:+.1f}%" if 'T10' in r else 'T10:未到達'
        t20 = f"{r['T20']:+.1f}%" if 'T20' in r else 'T20:未到達'
        print(f"[{rec['signal']}] {rec['company']}({rec['code']}) {rec['date']} @ {rec['price_at_signal']:,.0f}円")
        print(f"    {t5} / {t10} / {t20}")
    print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')


def _ttest_1samp(data: list[float]) -> tuple[float, float]:
    """1標本t検定 (H0: mean=0)。(t_stat, p_value) を返す。正規分布近似。"""
    n = len(data)
    if n < 2:
        return float('nan'), float('nan')
    m = sum(data) / n
    var = sum((x - m) ** 2 for x in data) / (n - 1)
    s = math.sqrt(var) if var > 0 else 0.0
    if s == 0:
        return float('nan'), float('nan')
    t = m / (s / math.sqrt(n))
    z = abs(t)
    p = 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))
    return round(t, 3), round(p, 4)


def _sharpe(returns_pct: list[float], n_days: int, rf_annual: float = 0.001) -> float:
    """保有期間ベースのSharpe比（年率換算）。"""
    n = len(returns_pct)
    if n < 2:
        return float('nan')
    rf = rf_annual * n_days / 252
    excess = [r / 100 - rf for r in returns_pct]
    m = sum(excess) / n
    s = math.sqrt(sum((x - m) ** 2 for x in excess) / (n - 1))
    if s == 0:
        return float('nan')
    return round(m / s * math.sqrt(252 / n_days), 3)


def cmd_stats(args: list[str]) -> None:
    records = load_tracker()
    if not records:
        print('記録がありません。')
        return

    from collections import defaultdict
    stats: dict = defaultdict(lambda: {'total': 0, 'wins': 0, 'sum_t5': [], 'sum_t10': [], 'sum_t20': []})

    for rec in records:
        sig = rec['signal']
        r = rec.get('returns', {})
        stats[sig]['total'] += 1
        if 'T5' in r:
            stats[sig]['sum_t5'].append(r['T5'])
            if r['T5'] > 0:
                stats[sig]['wins'] += 1
        if 'T10' in r:
            stats[sig]['sum_t10'].append(r['T10'])
        if 'T20' in r:
            stats[sig]['sum_t20'].append(r['T20'])

    print('\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    print(f'シグナル成績サマリー（総記録: {len(records)}件）')
    print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    print(f'{"Signal":<8} {"件数":>4} {"勝率":>6} {"T5平均":>8} {"T10平均":>8} {"T20平均":>8}')
    print('─' * 58)
    for sig in sorted(stats.keys()):
        s = stats[sig]
        t5_vals = s['sum_t5']
        t10_vals = s['sum_t10']
        t20_vals = s['sum_t20']
        win_rate = f"{s['wins']/len(t5_vals)*100:.0f}%" if t5_vals else '—'
        t5_avg = f"{sum(t5_vals)/len(t5_vals):+.1f}%" if t5_vals else '—'
        t10_avg = f"{sum(t10_vals)/len(t10_vals):+.1f}%" if t10_vals else '—'
        t20_avg = f"{sum(t20_vals)/len(t20_vals):+.1f}%" if t20_vals else '—'
        print(f'{sig:<8} {s["total"]:>4} {win_rate:>6} {t5_avg:>8} {t10_avg:>8} {t20_avg:>8}')

    # t検定・Sharpe詳細
    print('\n【統計検定】H0: 平均リターン = 0')
    print(f'{"Signal":<8} {"期間":>5} {"n":>4} {"t値":>7} {"p値":>7} {"有意":>5} {"Sharpe":>8}')
    print('─' * 50)
    for sig in sorted(stats.keys()):
        s = stats[sig]
        for label, vals, n_days in [('T5', s['sum_t5'], 5), ('T10', s['sum_t10'], 10), ('T20', s['sum_t20'], 20)]:
            if not vals:
                continue
            t_stat, p_val = _ttest_1samp(vals)
            sr = _sharpe(vals, n_days)
            sig_mark = '★ p<0.05' if (not math.isnan(p_val) and p_val < 0.05) else ('△ p<0.10' if (not math.isnan(p_val) and p_val < 0.10) else '—')
            t_str = f'{t_stat:+.3f}' if not math.isnan(t_stat) else '—'
            p_str = f'{p_val:.4f}' if not math.isnan(p_val) else '—'
            sr_str = f'{sr:.3f}' if not math.isnan(sr) else '—'
            print(f'{sig:<8} {label:>5} {len(vals):>4} {t_str:>7} {p_str:>7} {sig_mark:>5} {sr_str:>8}')

    print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    print('注: n<30 では正規分布近似のp値は参考値。★=p<0.05, △=p<0.10')


def cmd_list(args: list[str]) -> None:
    records = load_tracker()
    if not records:
        print('記録がありません。')
        return
    print(json.dumps(records, ensure_ascii=False, indent=2))


COMMANDS = {
    'record': cmd_record,
    'check': cmd_check,
    'stats': cmd_stats,
    'list': cmd_list,
}

if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print('Usage: tracker.py <record|check|stats|list> [options]')
        print('  record  --signal A --code 7203 --price 2500 [--company 名称]')
        print('  check   （JQuantsで損益計算・更新）')
        print('  stats   （シグナル別 勝率・平均リターン）')
        print('  list    （全記録表示）')
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
