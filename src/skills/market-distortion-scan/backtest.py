#!/usr/bin/env python3
"""
Signal D/E backtest ledger for market-distortion-scan.

Usage:
    python3 src/skills/market-distortion-scan/backtest.py collect [--date YYYYMMDD]
    python3 src/skills/market-distortion-scan/backtest.py daily
    python3 src/skills/market-distortion-scan/backtest.py print-cron
    python3 src/skills/market-distortion-scan/backtest.py install-cron
    python3 src/skills/market-distortion-scan/backtest.py update
    python3 src/skills/market-distortion-scan/backtest.py stats
    python3 src/skills/market-distortion-scan/backtest.py list

Notes:
    - collect stores all Signal D/E candidates, not only recommended names.
    - update uses J-Quants when JQUANTS_API_KEY is set in .env.
    - Signal D entry prices come from kabutan loser ranking at collection time.
      Historical --date collection for D is therefore only reliable when run
      on the same trading day as the ranking snapshot.
"""
import argparse
import json
import re
import ssl
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

SKILL_DIR = Path(__file__).parent
REPO_ROOT = SKILL_DIR.parent.parent.parent
RUN_SCAN = SKILL_DIR / 'run_scan.py'
BACKTEST_DIR = SKILL_DIR / 'results' / 'backtest'
LEDGER_FILE = BACKTEST_DIR / 'signals.json'
ENV_FILE = REPO_ROOT / '.env'
CHECKPOINTS = {'T1': 1, 'T3': 3, 'T5': 5, 'T10': 10, 'T20': 20}
AFTER_CLOSE_TIME = '15:30'
CRON_MARKER = '# kabu-dexter-market-distortion-backtest'

HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def load_env_key(name: str) -> str:
    if not ENV_FILE.exists():
        return ''
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        if line.startswith(f'{name}='):
            return line.split('=', 1)[1].strip().strip('"\'')
    return ''


def load_ledger() -> list[dict]:
    if not LEDGER_FILE.exists():
        return []
    return json.loads(LEDGER_FILE.read_text(encoding='utf-8'))


def save_ledger(records: list[dict]) -> None:
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')


def yyyymmdd_to_iso(value: str) -> str:
    return f'{value[:4]}-{value[4:6]}-{value[6:]}'


def nth_trading_day(from_date_str: str, n: int) -> str:
    d = date.fromisoformat(from_date_str)
    count = 0
    while count < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return d.isoformat()


def next_trading_day(from_date_str: str) -> str:
    return nth_trading_day(from_date_str, 1)


def parse_price_from_title(title: str) -> float | None:
    m = re.search(r'株価([\d,]+(?:\.\d+)?)円', title or '')
    if not m:
        return None
    try:
        return float(m.group(1).replace(',', ''))
    except ValueError:
        return None


def current_kabutan_price(code4: str) -> float | None:
    try:
        req = urllib.request.Request(f'https://kabutan.jp/stock/?code={code4}', headers=HEADERS)
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            html = r.read().decode('utf-8', errors='replace')
    except Exception:
        return None
    patterns = [
        r'<span class="kabuka">([\d,]+(?:\.\d+)?)</span>',
        r'現在値</th>\s*<td[^>]*>([\d,]+(?:\.\d+)?)',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.DOTALL)
        if m:
            try:
                return float(m.group(1).replace(',', ''))
            except ValueError:
                pass
    return None


def fetch_jquants_prices(code4: str, from_date: str, to_date: str, api_key: str) -> dict[str, float]:
    """Return ISO date -> adjusted close. Supports current and legacy endpoints."""
    code5 = code4 + '0'
    urls = [
        f'https://api.jquants.com/v1/prices/daily_quotes?code={code5}&from={from_date}&to={to_date}',
        f'https://api.jquants.com/v2/equities/bars/daily?code={code5}&from={from_date}&to={to_date}',
    ]
    for url in urls:
        req = urllib.request.Request(url, headers={'x-api-key': api_key, **HEADERS})
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                payload = json.loads(r.read())
        except Exception:
            continue

        rows = payload.get('daily_quotes') or payload.get('data') or []
        prices: dict[str, float] = {}
        for row in rows:
            d = row.get('Date') or row.get('date')
            close = row.get('AdjustmentClose') or row.get('AdjC') or row.get('Close')
            if d and close is not None:
                prices[d] = float(close)
        if prices:
            return prices
    return {}


def price_on_or_near(code4: str, target_date: str, api_key: str) -> tuple[str, float] | None:
    if not api_key:
        return None
    target = date.fromisoformat(target_date)
    start = (target - timedelta(days=3)).isoformat()
    end = (target + timedelta(days=3)).isoformat()
    prices = fetch_jquants_prices(code4, start, end, api_key)
    if not prices:
        return None
    ranked = sorted(
        prices.items(),
        key=lambda item: (abs((date.fromisoformat(item[0]) - target).days), item[0]),
    )
    return ranked[0]


def pead_entry_date(event_date: str, disclosure_time: str) -> tuple[str, str]:
    """Return entry date and rule label for PEAD using daily close data."""
    if disclosure_time and disclosure_time < AFTER_CLOSE_TIME:
        return event_date, 'same_day_close_before_1530'
    return next_trading_day(event_date), 'next_trading_day_close_after_1530'


def run_scan_json(scan_date: str | None) -> dict:
    args = ['python3', str(RUN_SCAN), '--json']
    if scan_date:
        args.extend(['--date', scan_date])
    proc = subprocess.run(args, cwd=REPO_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end='')
    if proc.returncode != 0:
        raise RuntimeError(f'run_scan.py failed with exit code {proc.returncode}')
    return json.loads(proc.stdout)


def is_recommended(signal: dict) -> bool:
    fin = signal.get('financials') or {}
    if signal.get('signal') == 'D':
        return bool(fin.get('roe') is not None and fin['roe'] >= 5 and fin.get('fcf') is not None and fin['fcf'] > 0)
    if signal.get('signal') == 'E':
        return bool(
            signal.get('composite_surprise') is not None
            and signal['composite_surprise'] >= 10
            and signal.get('pead_score') is not None
            and signal['pead_score'] >= 5
            and fin.get('roe') is not None
            and fin['roe'] >= 8
        )
    return False


def normalize_signal(signal: dict, scan_date: str, api_key: str) -> dict:
    signal_type = signal['signal']
    event_date = yyyymmdd_to_iso(signal.get('date') or scan_date)
    disclosure_time = signal.get('time', '')
    entry_date = event_date
    entry_basis = 'signal_day_close'

    if signal_type == 'E':
        entry_date, entry_basis = pead_entry_date(event_date, disclosure_time)

    price = signal.get('price') or parse_price_from_title(signal.get('title', ''))

    if price is None and api_key:
        fetched = price_on_or_near(signal['code'], entry_date, api_key)
        if fetched:
            price_date, price = fetched
            if price_date != entry_date:
                entry_basis = f'{entry_basis}_nearest_{price_date}'

    if price is None and entry_date == date.today().isoformat():
        price = current_kabutan_price(signal['code'])
        if price is not None:
            entry_basis = f'{entry_basis}_kabutan_current'

    return {
        'id': f'{event_date}:{signal_type}:{signal["code"]}',
        'collected_at': datetime.now().isoformat(timespec='seconds'),
        'event_date': event_date,
        'disclosure_time': disclosure_time,
        'signal': signal_type,
        'code': signal['code'],
        'company': signal.get('company') or signal.get('financials', {}).get('name') or signal['code'],
        'entry_date': entry_date,
        'entry_price': price,
        'entry_basis': entry_basis,
        'recommended': is_recommended(signal),
        'score': signal.get('score'),
        'reason': signal.get('reason', ''),
        'title': signal.get('title', ''),
        'metrics': {
            key: signal.get(key)
            for key in ['change_pct', 'pead_score', 'composite_surprise', 'text_score', 'current_change_pct']
            if key in signal
        },
        'financials': signal.get('financials', {}),
        'returns': {},
    }


def cmd_collect(args: argparse.Namespace) -> None:
    collect(args.date)


def collect(scan_date_arg: str | None) -> dict:
    payload = run_scan_json(scan_date_arg)
    scan_date = payload['scan_date']
    api_key = load_env_key('JQUANTS_API_KEY')
    raw_signals = []
    raw_signals.extend(payload.get('signal_d', []))
    raw_signals.extend(payload.get('signal_e', []))

    records = load_ledger()
    by_id = {rec['id']: rec for rec in records}
    added = updated = missing_price = 0

    for raw in raw_signals:
        rec = normalize_signal(raw, scan_date, api_key)
        if rec['entry_price'] is None:
            missing_price += 1
        if rec['id'] in by_id:
            existing = by_id[rec['id']]
            existing.update({k: v for k, v in rec.items() if k != 'returns'})
            updated += 1
        else:
            records.append(rec)
            by_id[rec['id']] = rec
            added += 1

    records.sort(key=lambda r: (r['event_date'], r['signal'], r['code']))
    save_ledger(records)
    summary = {
        'added': added,
        'updated': updated,
        'total': len(records),
        'missing_entry_price': missing_price,
        'ledger': str(LEDGER_FILE),
    }
    print(f'保存: {LEDGER_FILE}')
    print(
        f'collect: added={added} updated={updated} total={len(records)} '
        f'missing_entry_price={missing_price}'
    )
    return summary


def cmd_update(_args: argparse.Namespace) -> None:
    update_returns(require_api=True)


def update_returns(require_api: bool) -> dict:
    api_key = load_env_key('JQUANTS_API_KEY')
    if not api_key:
        message = 'JQUANTS_API_KEY が .env に設定されていません'
        if require_api:
            print(f'ERROR: {message}', file=sys.stderr)
            sys.exit(1)
        print(f'update skipped: {message}', file=sys.stderr)
        return {'updated_returns': 0, 'skipped_no_entry_price': 0, 'total': len(load_ledger()), 'skipped': True}

    records = load_ledger()
    today = date.today().isoformat()
    updated = skipped_no_entry = 0

    for rec in records:
        entry = rec.get('entry_price')
        if not entry:
            skipped_no_entry += 1
            continue
        returns = rec.setdefault('returns', {})
        for label, n in CHECKPOINTS.items():
            if label in returns:
                continue
            base_date = rec.get('entry_date') or rec['event_date']
            target_date = nth_trading_day(base_date, n)
            if target_date > today:
                continue
            fetched = price_on_or_near(rec['code'], target_date, api_key)
            if not fetched:
                continue
            price_date, close_price = fetched
            returns[label] = {
                'target_date': target_date,
                'price_date': price_date,
                'close': close_price,
                'return_pct': round((close_price - entry) / entry * 100, 2),
            }
            updated += 1

    save_ledger(records)
    summary = {
        'updated_returns': updated,
        'skipped_no_entry_price': skipped_no_entry,
        'total': len(records),
        'skipped': False,
    }
    print(f'update: updated_returns={updated} skipped_no_entry_price={skipped_no_entry} total={len(records)}')
    return summary


def cmd_daily(args: argparse.Namespace) -> None:
    collect_summary = collect(args.date)
    update_summary = update_returns(require_api=False)
    print('daily complete:')
    print(json.dumps({'collect': collect_summary, 'update': update_summary}, ensure_ascii=False, indent=2))


def build_cron_line(hour: int, minute: int) -> str:
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    log_file = BACKTEST_DIR / 'daily.log'
    command = (
        f'cd {REPO_ROOT} && {sys.executable} '
        f'{SKILL_DIR / "backtest.py"} daily >> {log_file} 2>&1'
    )
    return f'{minute} {hour} * * 1-5 {command} {CRON_MARKER}'


def cmd_print_cron(args: argparse.Namespace) -> None:
    print(build_cron_line(args.hour, args.minute))


def cmd_install_cron(args: argparse.Namespace) -> None:
    line = build_cron_line(args.hour, args.minute)
    current_proc = subprocess.run(['crontab', '-l'], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    current = current_proc.stdout if current_proc.returncode == 0 else ''
    kept = [existing for existing in current.splitlines() if CRON_MARKER not in existing]
    kept.append(line)
    new_crontab = '\n'.join(kept).rstrip() + '\n'
    install_proc = subprocess.run(['crontab', '-'], input=new_crontab, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if install_proc.returncode != 0:
        print(install_proc.stderr.strip() or 'ERROR: crontab install failed', file=sys.stderr)
        sys.exit(1)
    print('installed cron:')
    print(line)


def pct_values(records: list[dict], label: str) -> list[float]:
    values = []
    for rec in records:
        value = rec.get('returns', {}).get(label)
        if isinstance(value, dict) and 'return_pct' in value:
            values.append(float(value['return_pct']))
    return values


def summarize(values: list[float]) -> dict:
    if not values:
        return {'n': 0}
    ordered = sorted(values)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    wins = [v for v in values if v > 0]
    return {
        'n': len(values),
        'win_rate': len(wins) / len(values) * 100,
        'avg': sum(values) / len(values),
        'median': median,
        'min': min(values),
        'max': max(values),
    }


def cmd_stats(args: argparse.Namespace) -> None:
    records = load_ledger()
    if args.recommended_only:
        records = [r for r in records if r.get('recommended')]
    groups: dict[str, list[dict]] = defaultdict(list)
    for rec in records:
        groups[rec['signal']].append(rec)

    print(f'Backtest stats: records={len(records)} file={LEDGER_FILE}')
    if args.recommended_only:
        print('filter: recommended_only=true')
    print(f'{"Signal":<8} {"件数":>4} {"T":>4} {"n":>4} {"勝率":>7} {"平均":>8} {"中央値":>8} {"最小":>8} {"最大":>8}')
    print('-' * 75)
    for signal in sorted(groups):
        for label in CHECKPOINTS:
            summary = summarize(pct_values(groups[signal], label))
            if summary['n'] == 0:
                print(f'{signal:<8} {len(groups[signal]):>4} {label:>4} {0:>4} {"-":>7} {"-":>8} {"-":>8} {"-":>8} {"-":>8}')
            else:
                print(
                    f'{signal:<8} {len(groups[signal]):>4} {label:>4} {summary["n"]:>4} '
                    f'{summary["win_rate"]:>6.1f}% {summary["avg"]:>+7.2f}% {summary["median"]:>+7.2f}% '
                    f'{summary["min"]:>+7.2f}% {summary["max"]:>+7.2f}%'
                )


def cmd_list(args: argparse.Namespace) -> None:
    records = load_ledger()
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return
    for rec in records:
        entry = f'{rec["entry_price"]:,.1f}' if rec.get('entry_price') else 'NA'
        mark = 'REC' if rec.get('recommended') else '---'
        entry_date = rec.get('entry_date') or rec['event_date']
        basis = rec.get('entry_basis', '')
        print(
            f'[{rec["signal"]}] {rec["event_date"]} {rec["company"]}({rec["code"]}) '
            f'entry={entry}@{entry_date} {basis} {mark} {rec.get("reason", "")}'
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Backtest Signal D/E candidates.')
    sub = parser.add_subparsers(dest='command', required=True)

    collect = sub.add_parser('collect', help='Run scan and append Signal D/E candidates to ledger.')
    collect.add_argument('--date', help='YYYYMMDD scan date. D historical prices are only reliable for same-day collection.')
    collect.set_defaults(func=cmd_collect)

    daily = sub.add_parser('daily', help='Daily cron target: collect latest D/E candidates, then update matured returns.')
    daily.add_argument('--date', help='YYYYMMDD scan date. Usually omitted for scheduled runs.')
    daily.set_defaults(func=cmd_daily)

    print_cron = sub.add_parser('print-cron', help='Print a weekday crontab line for daily collection.')
    print_cron.add_argument('--hour', type=int, default=18, help='JST/local hour, default 18.')
    print_cron.add_argument('--minute', type=int, default=10, help='Minute, default 10.')
    print_cron.set_defaults(func=cmd_print_cron)

    install_cron = sub.add_parser('install-cron', help='Install/update a weekday crontab entry for daily collection.')
    install_cron.add_argument('--hour', type=int, default=18, help='JST/local hour, default 18.')
    install_cron.add_argument('--minute', type=int, default=10, help='Minute, default 10.')
    install_cron.set_defaults(func=cmd_install_cron)

    update = sub.add_parser('update', help='Fill T+1/T+3/T+5/T+10/T+20 returns using J-Quants.')
    update.set_defaults(func=cmd_update)

    stats = sub.add_parser('stats', help='Print grouped return statistics.')
    stats.add_argument('--recommended-only', action='store_true', help='Only include records that passed current recommendation filters.')
    stats.set_defaults(func=cmd_stats)

    list_cmd = sub.add_parser('list', help='List ledger records.')
    list_cmd.add_argument('--json', action='store_true', help='Print full ledger JSON.')
    list_cmd.set_defaults(func=cmd_list)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
