#!/usr/bin/env python3
"""
entry_backtest.py — tracker.json の記録を、シグナル翌営業日の仮想エントリーで検証する。

Usage:
    python3 entry_backtest.py
    python3 entry_backtest.py --compare
    python3 entry_backtest.py --entry next-open --as-of 2026-05-12
    python3 entry_backtest.py --rebound-pct 2.0 --compare

Notes:
    - 株価は kabutan.jp スマホ版の日足から取得する。
    - 「翌日安値+2%反発」は、翌営業日の安値 * 1.02 に到達したら約定とみなす。
      日足だけでは「安値を付けた後に反発した」順序は厳密に検証できない。
"""
from __future__ import annotations

import argparse
import html
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

SKILL_DIR = Path(__file__).parent
TRACKER_FILE = SKILL_DIR / "results" / "tracker.json"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


@dataclass(frozen=True)
class Bar:
    date: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None


@dataclass
class TradeResult:
    id: int
    signal_date: str
    signal: str
    code: str
    company: str
    top_group: bool
    entry_label: str
    entry_date: str
    entry_price: float
    eval_label: str
    eval_date: str
    eval_price: float
    pnl_per_share: float
    pct: float
    pnl_100: float


TOP_MARKERS = (
    "本命",
    "上位候補",
    "攻め",
    "総合1位候補",
    "次点候補",
    "守り重視の上位候補",
    "攻めの上位候補",
)


def load_tracker() -> list[dict]:
    if not TRACKER_FILE.exists():
        return []
    return json.loads(TRACKER_FILE.read_text(encoding="utf-8"))


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=ctx, timeout=15) as response:
        return response.read().decode("utf-8", errors="ignore")


def clean_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = re.sub(r"<.*?>", "", raw)
    text = html.unescape(text).replace(",", "").replace("\xa0", "").strip()
    if text in {"", "-", "--", "－"}:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else None


def normalize_company(name: str) -> str:
    return name.replace("Ｇ－", "").replace("ＨＤ", "HD").replace("　", "")


def fetch_bars(code: str, pause_sec: float) -> dict[str, Bar]:
    html_text = fetch(f"https://s.kabutan.jp/stocks/{code}/historical_prices/daily/")
    bars: dict[str, Bar] = {}

    # Historical table rows. Prefer rows that include an explicit datetime.
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html_text, re.S | re.I):
        dm = re.search(r'datetime=["\'](\d{4}-\d{2}-\d{2})["\']', row)
        if not dm:
            continue
        day = dm.group(1)
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S | re.I)
        nums = [clean_number(cell) for cell in cells]
        nums = [num for num in nums if num is not None]
        if len(nums) >= 4:
            bars[day] = Bar(day, nums[0], nums[1], nums[2], nums[3])

    # kabutan smartphone rows are currently M/D without year. They are listed
    # newest first, so months greater than the current month belong to last year.
    today = date.today()
    base_year = today.year
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html_text, re.S | re.I):
        th = next(iter(re.findall(r"<th\b[^>]*>(.*?)</th>", row, re.S | re.I)), "")
        text = re.sub(r"<.*?>", "", th).strip()
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", text)
        if not m:
            continue
        month = int(m.group(1))
        day_num = int(m.group(2))
        year = base_year - 1 if month > today.month else base_year
        day = date(year, month, day_num).isoformat()
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S | re.I)
        nums = [clean_number(cell) for cell in cells[:4]]
        if len(nums) == 4 and nums[3] is not None:
            bars[day] = Bar(day, nums[0], nums[1], nums[2], nums[3])

    if pause_sec > 0:
        time.sleep(pause_sec)
    return bars


def available_dates(bars_by_code: dict[str, dict[str, Bar]]) -> list[str]:
    dates = sorted({day for bars in bars_by_code.values() for day in bars})
    return dates


def next_market_date(dates: list[str], signal_date: str) -> str | None:
    for day in dates:
        if day > signal_date:
            return day
    return None


def nth_market_date(dates: list[str], start_date: str, n: int) -> str | None:
    later = [day for day in dates if day > start_date]
    if len(later) < n:
        return None
    return later[n - 1]


def last_market_date_on_or_before(dates: list[str], as_of: str) -> str | None:
    candidates = [day for day in dates if day <= as_of]
    return candidates[-1] if candidates else None


def is_top_group(record: dict) -> bool:
    deep_dive = record.get("deep_dive") or {}
    stance = str(deep_dive.get("stance") or "")
    thesis = str(record.get("buy_thesis") or "")
    return any(marker in stance or marker in thesis for marker in TOP_MARKERS)


def entry_price(record: dict, next_bar: Bar, mode: str, rebound_pct: float) -> tuple[str, float] | None:
    if mode == "signal-close":
        price = record.get("price_at_signal")
        if price is None:
            return None
        return "シグナル日終値", float(price)
    if mode == "next-open":
        if next_bar.open is None:
            return None
        return "翌日始値", next_bar.open
    if mode == "next-close":
        if next_bar.close is None:
            return None
        return "翌日終値", next_bar.close
    if mode == "rebound-low-plus":
        if next_bar.low is None or next_bar.high is None:
            return None
        price = round(next_bar.low * (1 + rebound_pct / 100), 3)
        if price > next_bar.high:
            return None
        return f"翌日安値+{rebound_pct:g}%反発", price
    raise ValueError(f"unknown entry mode: {mode}")


def eval_date_for(label: str, all_dates: list[str], next_date: str, as_of: str) -> tuple[str, str] | None:
    if label == "T1":
        return "T1", next_date
    if label in {"T3", "T5"}:
        n = int(label[1:])
        day = nth_market_date(all_dates, next_date, n)
        return (label, day) if day else None
    if label == "TODAY":
        day = last_market_date_on_or_before(all_dates, as_of)
        return ("TODAY", day) if day else None
    raise ValueError(f"unknown eval label: {label}")


def build_results(
    records: list[dict],
    bars_by_code: dict[str, dict[str, Bar]],
    entry_mode: str,
    eval_label: str,
    as_of: str,
    rebound_pct: float,
) -> tuple[list[TradeResult], list[int]]:
    all_dates = available_dates(bars_by_code)
    results: list[TradeResult] = []
    skipped: list[int] = []

    for record in records:
        code = str(record.get("code", ""))
        bars = bars_by_code.get(code, {})
        signal_date = str(record.get("date", ""))
        next_date = next_market_date(all_dates, signal_date)
        next_bar = bars.get(next_date) if next_date else None
        if entry_mode != "signal-close":
            if not next_date or not next_bar:
                skipped.append(int(record.get("id", 0)))
                continue

        entry = entry_price(record, next_bar, entry_mode, rebound_pct)
        if not entry:
            skipped.append(int(record.get("id", 0)))
            continue
        entry_name, entry_px = entry

        eval_start_date = next_date if next_date else signal_date
        eval_info = eval_date_for(eval_label, all_dates, eval_start_date, as_of)
        if not eval_info:
            skipped.append(int(record.get("id", 0)))
            continue
        eval_name, eval_date = eval_info
        eval_bar = bars.get(eval_date)
        if not eval_bar or eval_bar.close is None:
            skipped.append(int(record.get("id", 0)))
            continue

        pnl = eval_bar.close - entry_px
        pct = pnl / entry_px * 100
        results.append(
            TradeResult(
                id=int(record.get("id", 0)),
                signal_date=signal_date,
                signal=str(record.get("signal", "")),
                code=code,
                company=normalize_company(str(record.get("company") or code)),
                top_group=is_top_group(record),
                entry_label=entry_name,
                entry_date=next_date if entry_mode != "signal-close" else signal_date,
                entry_price=entry_px,
                eval_label=eval_name,
                eval_date=eval_date,
                eval_price=eval_bar.close,
                pnl_per_share=pnl,
                pct=pct,
                pnl_100=pnl * 100,
            )
        )
    return results, skipped


def summarize(rows: list[TradeResult]) -> dict[str, float | int | str]:
    if not rows:
        return {"n": 0, "win_rate": "NA", "avg": "NA", "median": "NA", "total100": 0}
    pcts = sorted(row.pct for row in rows)
    mid = len(pcts) // 2
    median = pcts[mid] if len(pcts) % 2 else (pcts[mid - 1] + pcts[mid]) / 2
    return {
        "n": len(rows),
        "win_rate": sum(1 for row in rows if row.pnl_per_share > 0) / len(rows) * 100,
        "avg": sum(row.pct for row in rows) / len(rows),
        "median": median,
        "total100": sum(row.pnl_100 for row in rows),
    }


def width(text: str) -> int:
    total = 0
    for ch in str(text):
        total += 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1
    return total


def pad(text: str, size: int, align: str = "left") -> str:
    text = str(text)
    diff = max(0, size - width(text))
    if align == "right":
        return " " * diff + text
    return text + " " * diff


def fmt_yen(value: float) -> str:
    return f"{value:,.0f}"


def fmt_px(value: float) -> str:
    return f"{value:,.1f}" if value % 1 else f"{value:,.0f}"


def fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


def print_trade_table(rows: list[TradeResult]) -> None:
    specs = [
        ("ID", 3, "right"),
        ("発生日", 10, "left"),
        ("S", 1, "left"),
        ("code", 4, "left"),
        ("銘柄", 16, "left"),
        ("Entry日", 10, "left"),
        ("Entry", 9, "right"),
        ("評価日", 10, "left"),
        ("終値", 9, "right"),
        ("損益率", 8, "right"),
        ("100株損益", 11, "right"),
    ]
    print(" ".join(pad(name, size, align) for name, size, align in specs))
    print("-" * 112)
    for row in rows:
        values = [
            row.id,
            row.signal_date,
            row.signal,
            row.code,
            row.company[:16],
            row.entry_date,
            fmt_px(row.entry_price),
            row.eval_date,
            fmt_px(row.eval_price),
            fmt_pct(row.pct),
            fmt_yen(row.pnl_100),
        ]
        print(" ".join(pad(value, size, align) for value, (_, size, align) in zip(values, specs)))


def print_summary_line(label: str, rows: list[TradeResult]) -> None:
    s = summarize(rows)
    if not rows:
        print(f"{pad(label, 18)} n= 0")
        return
    print(
        f"{pad(label, 18)} "
        f"n={int(s['n']):2d} "
        f"win={float(s['win_rate']):5.1f}% "
        f"avg={float(s['avg']):+6.2f}% "
        f"med={float(s['median']):+6.2f}% "
        f"total100={float(s['total100']):+,.0f}円"
    )


def print_group_summaries(rows: list[TradeResult]) -> None:
    print("\n# 集計")
    print_summary_line("全件", rows)
    print_summary_line("本命+上位候補", [row for row in rows if row.top_group])
    for signal in sorted({row.signal for row in rows}):
        print_summary_line(f"Signal {signal}", [row for row in rows if row.signal == signal])
    for signal_date in sorted({row.signal_date for row in rows}):
        print_summary_line(signal_date, [row for row in rows if row.signal_date == signal_date])


def collect_bars(records: list[dict], pause_sec: float) -> dict[str, dict[str, Bar]]:
    bars_by_code: dict[str, dict[str, Bar]] = {}
    for code in sorted({str(record.get("code")) for record in records if record.get("code")}):
        bars_by_code[code] = fetch_bars(code, pause_sec)
    return bars_by_code


def main() -> int:
    parser = argparse.ArgumentParser(description="tracker.json の翌営業日エントリー損益を検証する")
    parser.add_argument(
        "--entry",
        choices=["signal-close", "next-open", "next-close", "rebound-low-plus"],
        default="next-open",
        help="エントリー条件。既定は翌日始値。",
    )
    parser.add_argument("--as-of", default=date.today().isoformat(), help="評価基準日 YYYY-MM-DD")
    parser.add_argument("--eval", choices=["T1", "T3", "T5", "TODAY"], default="TODAY")
    parser.add_argument("--rebound-pct", type=float, default=2.0)
    parser.add_argument("--compare", action="store_true", help="エントリー条件と評価時点を一括比較する")
    parser.add_argument("--pause", type=float, default=0.2, help="kabutan取得間隔秒")
    args = parser.parse_args()

    records = load_tracker()
    if not records:
        print("記録がありません。")
        return 1

    bars_by_code = collect_bars(records, args.pause)

    rows, skipped = build_results(
        records,
        bars_by_code,
        args.entry,
        args.eval,
        args.as_of,
        args.rebound_pct,
    )
    rows.sort(key=lambda row: row.id)

    print(f"# 翌営業日エントリー損益検証 as_of={args.as_of} entry={args.entry} eval={args.eval}")
    print_trade_table(rows)
    print_group_summaries(rows)

    if args.compare:
        print("\n# エントリー条件比較（TODAY終値評価）")
        for mode in ["signal-close", "next-open", "next-close", "rebound-low-plus"]:
            comp_rows, _ = build_results(records, bars_by_code, mode, "TODAY", args.as_of, args.rebound_pct)
            label = {
                "signal-close": "シグナル日終値",
                "next-open": "翌日始値",
                "next-close": "翌日終値",
                "rebound-low-plus": f"翌日安値+{args.rebound_pct:g}%反発",
            }[mode]
            print_summary_line(label, comp_rows)

        print("\n# 評価時点比較（翌日始値エントリー）")
        for label in ["T1", "T3", "T5", "TODAY"]:
            comp_rows, _ = build_results(records, bars_by_code, "next-open", label, args.as_of, args.rebound_pct)
            print_summary_line(label, comp_rows)

    if skipped:
        print(f"\n未到来または株価不足で除外したID: {skipped}")

    print(
        "\n注: 翌日安値+反発条件は日足ベースの簡易判定。"
        "日中に安値を付けた後で反発した順序までは検証していない。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
