#!/usr/bin/env python3
"""
market-distortion-scan / run_scan.py
歪みスキャン ワンコマンド実行スクリプト。

Usage:
    python3 src/skills/market-distortion-scan/run_scan.py [--date YYYYMMDD] [--signal A|B|C|D|E|F|G] [--pead-days N] [--json]
    python3 src/skills/market-distortion-scan/run_scan.py --signal F --json

Outputs:
    - stdout: テキストレポート
    - src/skills/market-distortion-scan/results/YYYY-MM-DD.md
"""
import json
import re
import ssl
import subprocess
import sys
import time
import unicodedata
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from large_holdings import extract_from_tdnet
from margin import scan_margin_signals
from pead import prev_biz_days, scan_pead

SKILL_DIR = Path(__file__).parent

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
BAD_KEYWORDS = ['下方修正', '業績悪化', '不正', '調査', '損失', '引当', '減損', '訂正', '行政処分']
ETF_EXCLUDE_KEYWORDS = ['ベア', 'ブル', 'インバース', 'レバレッジ', 'ETF', 'ETN', '先物', 'NEXT FUNDS', 'iFreeETF', '上場投信']


# ── ユーティリティ ─────────────────────────────────────────────────────────────

def get(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='replace')


# ── Step 1: 直近取引日を TDNet で特定 ──────────────────────────────────────────

def last_trading_date() -> str:
    d = date.today()
    for _ in range(10):
        if d.weekday() < 5:  # 月〜金
            ds = d.strftime('%Y%m%d')
            try:
                html = get(f'https://www.release.tdnet.info/inbs/I_list_001_{ds}.html')
                if '開示された情報はありません' not in html and len(html) > 3000:
                    return ds
            except Exception:
                pass
        d -= timedelta(days=1)
    raise RuntimeError('TDNetから直近取引日を特定できませんでした')


# ── Step 2: TDNet 適時開示取得（全ページ） ────────────────────────────────────

def fetch_tdnet_disclosures(date_str: str) -> dict:
    items = []
    for page in range(1, 15):
        try:
            html = get(f'https://www.release.tdnet.info/inbs/I_list_{page:03d}_{date_str}.html')
        except Exception:
            break

        if '開示された情報はありません' in html:
            break

        for row in re.findall(r'<tr>(.*?)</tr>', html, re.DOTALL):
            time_m = re.search(r'class="[^"]*kjTime[^"]*"[^>]*>(\d{2}:\d{2})', row)
            code_m = re.search(r'class="[^"]*kjCode[^"]*"[^>]*>\s*(\d{4,5})\s*', row)
            name_m = re.search(r'class="[^"]*kjName[^"]*"[^>]*>(.*?)</td>', row, re.DOTALL)
            title_m = re.search(r'class="[^"]*kjTitle[^"]*".*?<a[^>]+>([^<]+)</a>', row, re.DOTALL)
            url_m = re.search(r'class="[^"]*kjTitle[^"]*".*?<a[^>]+href="([^"]+)"', row, re.DOTALL)
            if code_m and title_m:
                href = url_m.group(1) if url_m else ''
                tdnet_url = ('https://www.release.tdnet.info' + href) if href.startswith('/') else href
                items.append({
                    'code': code_m.group(1)[:4],
                    'companyName': re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', name_m.group(1))).strip() if name_m else '',
                    'title': title_m.group(1).strip(),
                    'time': time_m.group(1) if time_m else '',
                    'date': date_str,
                    'tdnet_url': tdnet_url,
                })

        total_m = re.search(r'全(\d+)件', html)
        if total_m and page * 100 >= int(total_m.group(1)):
            break
        time.sleep(0.2)

    return {'items': items}


# ── Step 3: kabutan.jp 値下がりランキング（Signal D 用） ──────────────────────

def fetch_losers(threshold: float = -8.0) -> list:
    try:
        html = get('https://kabutan.jp/warning/?mode=2_2')
    except Exception:
        return []

    tables = re.findall(r'<table[^>]*>(.*?)</table>', html, re.DOTALL)
    if len(tables) < 3:
        return []

    results = []
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', tables[2], re.DOTALL)[1:]:
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)
        text = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        code_m = re.search(r'/stock/\?code=(\d{4})', row)
        if not code_m:
            continue
        code = code_m.group(1)
        name = re.sub(r'\s+', '', text[1]) if len(text) > 1 else ''
        price = 0.0
        for t in text[4:7]:
            tc = t.replace(',', '')
            if re.match(r'^\d+(\.\d+)?$', tc):
                price = float(tc)
                break
        change_pct = None
        for t in text:
            m = re.match(r'^([+-]\d+\.\d+)%$', t)
            if m:
                change_pct = float(m.group(1))
                break
        if change_pct is None or change_pct > threshold:
            continue
        volume = 0
        for t in text:
            tc = t.replace(',', '')
            if re.match(r'^\d{4,}$', tc):
                volume = int(tc)
                break
        if volume == 0:
            continue
        results.append({'code': code, 'name': name, 'change_pct': change_pct, 'price': price})
    return results


# ── Step 4: シグナル分類 ───────────────────────────────────────────────────────

def score_a(item: dict):
    t = item.get('title', '')
    if '下方修正' in t:
        return None, None
    if '大幅上方修正' in t:
        return 90, '大幅上方修正'
    if '上方修正' in t and '業績' in t:
        return 75, '業績予想上方修正'
    if '上方修正' in t:
        return 65, '上方修正'
    if '業績修正' in t and '訂正' not in t:
        return 60, '業績予想修正'
    return None, None


def score_c(item: dict):
    t = item.get('title', '')
    if any(k in t for k in ['取得状況', '消却', '処分']):
        return None, None
    if '自己株式の取得' in t or '自己の株式の取得' in t:
        return 70, '自社株買い取得決議'
    if '自己株式取得' in t:
        return 70, '自社株買い取得決議'
    if '自社株' in t and '取得' in t:
        return 65, '自社株買い'
    return None, None


def score_d(loser: dict, disc_by_code: dict):
    code = loser['code']
    pct = loser['change_pct']
    related = disc_by_code.get(code, [])
    for item in related:
        if any(k in item.get('title', '') for k in BAD_KEYWORDS):
            return None, None
    score = 90 if pct <= -20 else (80 if pct <= -15 else 70)
    if not related:
        score = min(score + 10, 100)
        reason = f'前日比{pct:.1f}% / 開示なし'
    else:
        reason = f'前日比{pct:.1f}% / 悪材料なし'
    return score, reason


def classify(disclosures: dict, losers: list) -> list:
    disc_by_code: dict = {}
    for item in disclosures.get('items', []):
        disc_by_code.setdefault(item['code'], []).append(item)

    results = []
    for item in disclosures.get('items', []):
        code = item.get('code', '')
        company = item.get('companyName', '')
        title = item.get('title', '')
        for score_fn, sig in [(score_a, 'A'), (score_c, 'C')]:
            sc, reason = score_fn(item)
            if sc is not None:
                results.append({'signal': sig, 'code': code, 'company': company, 'title': title, 'score': sc, 'reason': reason})

    results.extend(extract_from_tdnet(disclosures))

    for loser in losers:
        if any(kw in loser.get('name', '') for kw in ETF_EXCLUDE_KEYWORDS):
            continue
        sd, rd = score_d(loser, disc_by_code)
        if sd is not None:
            results.append({
                'signal': 'D', 'code': loser['code'], 'company': loser['name'],
                'title': f'前日比{loser["change_pct"]:.1f}% / 株価{loser["price"]:,.0f}円',
                'score': sd, 'reason': rd, 'change_pct': loser['change_pct'],
                'price': loser['price'],
            })

    seen: dict = {}
    for r in sorted(results, key=lambda x: x['score'], reverse=True):
        key = (r['signal'], r['code'])
        if key not in seen:
            seen[key] = r
    return sorted(seen.values(), key=lambda x: x['score'], reverse=True)


# ── Step 5: kabutan.jp 財務データ（上位5件のみ） ──────────────────────────────

def _extract_tbody_first_row_td(html: str, marker: str, td_index: int) -> float | None:
    m = re.search(rf'{re.escape(marker)}.*?<tbody>(.*?)</tbody>', html, re.DOTALL)
    if not m:
        return None
    # 2グループで (th_content, tds_content) を取得
    rows = re.findall(r'<tr\s*[^>]*>\s*<th[^>]*>(.*?)</th>(.*?)</tr>', m.group(1), re.DOTALL)
    for _th, tds_html in rows:
        tds = re.findall(r'<td[^>]*>([-\d,.]+)</td>', tds_html)
        if len(tds) > td_index:
            try:
                return float(tds[td_index].replace(',', ''))
            except ValueError:
                pass
    return None


def fetch_financials(code4: str) -> dict:
    result: dict = {'code': code4}
    try:
        html = get(f'https://kabutan.jp/stock/?code={code4}')
        name_m = re.search(r'<title>([^【]+)【', html)
        if name_m:
            result['name'] = name_m.group(1).strip().split('（')[0].strip()
        price_m = re.search(r'現在値</th>\s*<td>([\d,.]+)', html)
        if not price_m:
            price_m = re.search(r'前日終値</dt>\s*<dd[^>]*>([\d,.]+)', html)
        if price_m:
            result['price'] = float(price_m.group(1).replace(',', ''))
        per_pbr_m = re.search(
            r'data-help="PER"[^>]*>PER.*?<tbody.*?<td>([\d.]+)<span[^>]*>倍.*?<td>([\d.]+)<span[^>]*>倍.*?<td>([\d.]+)<span[^>]*>％',
            html, re.DOTALL
        )
        if per_pbr_m:
            result['per'] = float(per_pbr_m.group(1))
            result['pbr'] = float(per_pbr_m.group(2))
            result['dividend_yield_pct'] = float(per_pbr_m.group(3))
        # 時価総額: 値と単位を両方キャプチャして百万円に正規化
        # HTML例: <td>40.6<span>億円</span></td>
        cap_m = re.search(r'時価総額[^<]*</t[dh]>.*?<td[^>]*>([\d,.]+)<span>(億円|百万円|兆円)</span>', html, re.DOTALL)
        if cap_m:
            try:
                raw = float(cap_m.group(1).replace(',', ''))
                unit = cap_m.group(2)
                if '兆円' in unit:
                    result['mktcap'] = int(raw * 1_000_000)  # 兆円→百万円
                elif '億円' in unit:
                    result['mktcap'] = int(raw * 100)         # 億円→百万円
                else:
                    result['mktcap'] = int(raw)               # 百万円のまま
            except ValueError:
                pass
    except Exception:
        pass

    time.sleep(0.3)

    try:
        hist = get(f'https://s.kabutan.jp/stocks/{code4}/historical_prices/daily/')
        start = hist.find('52週高値')
        end = hist.find('年初来高値')
        block = hist[start:end if end != -1 else start + 1000] if start != -1 else ''
        values = re.findall(
            r'<span class="text-lg">([\d,]+(?:\.\d+)?)</span>\s*<span>\(([^)]+)\)</span>',
            block,
            re.DOTALL,
        )
        if values:
            week52_high = float(values[0][0].replace(',', ''))
            result['week52_high'] = week52_high
            result['week52_high_date'] = values[0][1]
            price = result.get('price')
            if price:
                result['drawdown_pct'] = round((price - week52_high) / week52_high * 100, 1)
    except Exception:
        pass

    time.sleep(0.3)

    try:
        html = get(f'https://kabutan.jp/stock/finance/?code={code4}')
        # ROE: 収益性テーブルの3番目のtd（売上高, 経常益, ROE, ROA...）
        roe = _extract_tbody_first_row_td(html, 'data-help="ROE"', 2)
        if roe is not None:
            result['roe'] = roe
        # FCF: フリーCFテーブルの1番目のtd
        fcf = _extract_tbody_first_row_td(html, 'data-help="フリーCF"', 0)
        if fcf is not None:
            result['fcf'] = fcf
    except Exception:
        pass

    time.sleep(0.3)
    return result


# ── Step 6: 昇格判定 ──────────────────────────────────────────────────────────

def fuse_signals(signals: list) -> list:
    """
    同一銘柄が複数シグナルで同時検出された場合に fusion_score を付与する。
    2シグナル以上で +20 ボーナス。

    Returns:
        fusion_signals: [{code, company, signals, fusion_score, items}, ...]
        スコア降順でソート済み。
    """
    by_code: dict = {}
    for s in signals:
        code = s['code']
        if code not in by_code:
            by_code[code] = []
        by_code[code].append(s)

    result = []
    for code, items in by_code.items():
        if len(set(s['signal'] for s in items)) < 2:
            continue
        sig_names = sorted(set(s['signal'] for s in items))
        total_score = sum(s['score'] for s in items) + 20  # fusion bonus
        company = items[0].get('company', code)
        result.append({
            'code': code,
            'company': company,
            'signals': sig_names,
            'fusion_score': total_score,
            'items': items,
        })

    result.sort(key=lambda x: x['fusion_score'], reverse=True)
    return result


def evaluate(signal: str, fin: dict) -> tuple[bool, str]:
    roe = fin.get('roe')
    fcf = fin.get('fcf')
    pbr = fin.get('pbr')
    mktcap = fin.get('mktcap')  # 百万円

    def roe_str():
        return f'ROE {roe:.1f}%{"✓" if roe and roe >= 5 else "✗"}' if roe is not None else ''

    def fcf_str():
        return f'FCF {"黒字✓" if fcf and fcf > 0 else "赤字✗"}' if fcf is not None else ''

    def cap_str():
        if mktcap is None:
            return ''
        cap_oku = mktcap / 100
        if 5000 <= mktcap <= 50000:
            return f'時価総額{cap_oku:.0f}億★'  # 50〜500億: 小型有望
        return f'時価総額{cap_oku:.0f}億'

    if signal == 'A':
        ok = bool(roe and roe >= 8 and fcf and fcf > 0)
        return ok, ' / '.join(filter(None, [roe_str(), fcf_str(), cap_str()]))
    elif signal == 'B':
        # 小型株（50〜500億）で機関新規は情報優位が大きい
        ok = mktcap is not None and 5000 <= mktcap <= 50000
        return ok, ' / '.join(filter(None, [cap_str()]))
    elif signal == 'C':
        ok = bool(pbr and pbr < 1.0)
        pbr_s = f'PBR {pbr:.2f}{"✓" if ok else "✗"}' if pbr is not None else ''
        return ok, ' / '.join(filter(None, [pbr_s, fcf_str(), cap_str()]))
    elif signal == 'D':
        per = fin.get('per')
        dividend_yield = fin.get('dividend_yield_pct')
        drawdown = fin.get('drawdown_pct')
        ok = bool(
            roe and roe >= 5
            and fcf and fcf > 0
            and per is not None and per < 20
            and pbr is not None and pbr < 2.0
            and dividend_yield is not None and dividend_yield >= 3.0
            and mktcap is not None and 5000 <= mktcap <= 50000
            and (drawdown is None or drawdown >= -40.0)
        )
        pbr_s = f'PBR {pbr:.2f}{"✓" if pbr < 2.0 else "✗"}' if pbr is not None else ''
        per_s = f'PER {per:.1f}{"✓" if per < 20 else "✗"}' if per is not None else ''
        yield_s = f'利回り {dividend_yield:.2f}%{"✓" if dividend_yield >= 3.0 else "✗"}' if dividend_yield is not None else ''
        drawdown_s = f'52週高値比 {drawdown:.1f}%{"✓" if drawdown >= -40.0 else "✗"}' if drawdown is not None else ''
        return ok, ' / '.join(filter(None, [roe_str(), fcf_str(), per_s, pbr_s, yield_s, drawdown_s, cap_str()]))
    elif signal == 'E':
        ok = bool(roe and roe >= 8)
        return ok, ' / '.join(filter(None, [roe_str(), fcf_str(), cap_str()]))
    elif signal == 'G':
        ok = bool(roe and roe >= 5 and mktcap and mktcap <= 100000)
        return ok, ' / '.join(filter(None, [roe_str(), cap_str()]))
    return False, ''


# ── Step 7: レポート整形 ──────────────────────────────────────────────────────

SIGNAL_LABELS = {
    'A': '業績上方修正', 'B': '大量保有出現', 'C': '自社株買い',
    'D': '暴落過剰反応', 'E': 'PEAD 決算サプライズドリフト',
    'F': 'メディアアルファ', 'G': '信用需給ひっ迫',
}


def format_report(signals: list, financials: dict, scan_date: str, total_disc: int) -> str:
    sd = f'{scan_date[:4]}/{scan_date[4:6]}/{scan_date[6:]}'
    lines = [
        '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
        f'本日の市場歪みシグナル  {date.today().isoformat()}（開示日: {sd}）',
        '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
        '',
    ]

    by_signal: dict = {key: [] for key in SIGNAL_LABELS}
    for s in signals:
        by_signal.setdefault(s['signal'], []).append(s)

    # Fusion シグナルセクション
    fusion_signals = fuse_signals(signals)
    if fusion_signals:
        lines.append('=== FUSION シグナル（複数同時発火） ===')
        for fs in fusion_signals:
            code = fs['code']
            sig_str = '+'.join(fs['signals'])
            fin = financials.get(code, {})
            has_fin = bool(fin)
            # 各構成シグナルがバリデーションを通過するか確認
            all_pass = has_fin and all(evaluate(sig, fin)[0] for sig in fs['signals'])
            if all_pass:
                label = '★最優先'
            else:
                blocking = []
                if not has_fin:
                    blocking.append('財務データなし')
                else:
                    for sig in fs['signals']:
                        ok, _ = evaluate(sig, fin)
                        if not ok:
                            blocking.append(f'Signal {sig} 未通過')
                label = f'複合要確認（{", ".join(blocking)}）'
            lines.append(f'  ● {fs["company"]}({fs["code"]}) — [{sig_str}] fusion_score: {fs["fusion_score"]}  {label}')
        lines.append('')

    recommended = 0
    for sig_key in ['A', 'B', 'C', 'D', 'E', 'G']:
        items = by_signal[sig_key]
        lines.append(f'{sig_key}: {SIGNAL_LABELS[sig_key]}')
        if not items:
            lines.append('  シグナルなし')
        else:
            for item in items:
                code = item['code']
                has_financials = code in financials
                fin = financials.get(code, {})
                is_rec, fin_note = evaluate(item['signal'], fin)
                if not has_financials:
                    item['financials_missing'] = True
                if is_rec:
                    recommended += 1
                name = fin.get('name', item.get('company', code))
                parts = [item['reason']]
                if not has_financials:
                    parts.append('財務データなし')
                if fin_note:
                    parts.append(fin_note)
                if 'per' in fin:
                    parts.append(f'PER {fin["per"]:.1f}x')
                if 'pbr' in fin and item['signal'] not in ('C',):
                    parts.append(f'PBR {fin["pbr"]:.2f}')
                if 'dividend_yield_pct' in fin:
                    parts.append(f'利回り {fin["dividend_yield_pct"]:.2f}%')
                if item['signal'] == 'E':
                    cs = item.get('composite_surprise')
                    ps = item.get('pead_score')
                    cp = item.get('current_change_pct')
                    if cs is not None:
                        parts.append(f'サプライズ{cs:.1f}%')
                    if ps is not None:
                        parts.append(f'PEADスコア{ps:.1f}')
                    if cp is not None:
                        parts.append(f'当日変化{cp:.1f}%')
                detail = ' / '.join(parts)
                if item['signal'] == 'C' and not is_rec and fin.get('pbr') is not None:
                    mark = '  [除外: PBR≥1.0]'
                else:
                    mark = '  ★推奨' if is_rec else ('  → 財務要注意' if fin_note and '✗' in fin_note else '')
                lines.append(f'  ● {name}({code}) — {detail}{mark}')
        lines.append('')

    fusion_count = len(fusion_signals)
    lines += [
        '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
        f'★推奨: {recommended}件  |  FUSION: {fusion_count}件  |  スキャン対象: 適時開示{total_disc}件 / 暴落{len(by_signal["D"])}件 / PEAD{len(by_signal["E"])}件 / 信用{len(by_signal["G"])}件',
        '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',
    ]
    return '\n'.join(lines)


# ── JSON 出力（Claude アナリスト用） ──────────────────────────────────────────

def output_json(signals: list, financials: dict, disclosures: dict, scan_date: str) -> None:
    disc_by_code: dict = {}
    for item in disclosures.get('items', []):
        disc_by_code.setdefault(item['code'], []).append(item)

    def enrich(s: dict) -> dict:
        r = dict(s)
        has_financials = s['code'] in financials
        fin = financials.get(s['code'], {})
        if not has_financials:
            r['financials_missing'] = True
        if fin:
            r['financials'] = {k: v for k, v in fin.items() if k != 'code'}
        is_rec, fin_note = evaluate(s['signal'], fin)
        r['recommended'] = bool(has_financials and is_rec)
        if fin_note:
            r['recommendation_note'] = fin_note
        # Signal A: TDNet文書URLを含める（Claudeが確認用）
        if s['signal'] == 'A':
            disc_items = disc_by_code.get(s['code'], [])
            for d in disc_items:
                if d.get('title') == s.get('title'):
                    r['tdnet_url'] = d.get('tdnet_url', '')
                    break
        # Signal D: 同日開示リストを含める（Claudeが悪材料判断用）
        if s['signal'] == 'D':
            r['same_day_disclosures'] = [
                {'title': d['title'], 'time': d['time'], 'tdnet_url': d.get('tdnet_url', '')}
                for d in disc_by_code.get(s['code'], [])
            ]
        return r

    signal_a = [enrich(s) for s in signals if s['signal'] == 'A']
    signal_b = [enrich(s) for s in signals if s['signal'] == 'B']
    signal_c = [enrich(s) for s in signals if s['signal'] == 'C']
    signal_d = [enrich(s) for s in signals if s['signal'] == 'D']
    signal_e = [enrich(s) for s in signals if s['signal'] == 'E']
    signal_f: list = []
    signal_g = [enrich(s) for s in signals if s['signal'] == 'G']
    fusion_signals = fuse_signals(signals)

    out = {
        'scan_date': scan_date,
        'total_disclosures': len(disclosures.get('items', [])),
        'signal_a': signal_a,
        'signal_b': signal_b,
        'signal_c': signal_c,
        'signal_d': signal_d,
        'signal_e': signal_e,
        'signal_f': signal_f,
        'signal_g': signal_g,
        'fusion_signals': [
            {
                'code': fs['code'],
                'company': fs['company'],
                'signals': fs['signals'],
                'fusion_score': fs['fusion_score'],
            }
            for fs in fusion_signals
        ],
        '_instructions': {
            'signal_a': 'tdnet_url にアクセスして開示文書を読み、上方/下方を確認。上方のみ採用。質スコア(1-5)・買いテーゼ・出口条件・リスクを生成する',
            'signal_b': 'tdnet_url にアクセスして保有者名・比率を確認。機関投資家の新規取得 or 増加のみ採用。financials.mktcap が 5000〜50000（50〜500億）の銘柄を優先',
            'signal_c': 'PBR < 1.0 かつ取得規模（取得総額/時価総額）で重み付け',
            'signal_d': 'same_day_disclosures を読んで真の悪材料（不正・減損・下方修正）がないか確認。D推奨は PER < 20、PBR < 2.0、配当利回り >= 3%、時価総額50〜500億、52週高値比-40%以内に限定。それ以外の急落は監視扱い',
            'signal_e': 'PEAD候補。pead_score・composite_surprise・text_score を確認し、ROE >= 8% かつ composite_surprise >= 10% の銘柄を優先採用。tdnet_url で決算内容を確認する',
            'signal_f': 'メディアアルファ候補。YouTube投資系チャンネルのテーマと銘柄インパクトを別途抽出する',
            'signal_g': '信用需給ひっ迫候補。margin_ratio >= 3.0 または前週比+50%以上の銘柄。需給ひっ迫による株価上昇圧力あり',
            'fusion_signals': '複数シグナルで同時検出された最優先候補。signals に含まれる各シグナルの詳細を参照して複合判断する',
        },
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


# ── main ──────────────────────────────────────────────────────────────────────

def run_media_alpha(json_mode: bool, channel_id: str | None = None) -> None:
    script = SKILL_DIR.parent / 'media-alpha' / 'run_weekly.py'
    args = ['python3', str(script)]
    if json_mode:
        args.append('--json')
    if channel_id:
        args.extend(['--channel', channel_id])

    proc = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end='')
    if proc.stdout:
        print(proc.stdout, end='')
    if proc.returncode != 0:
        sys.exit(proc.returncode)


def main():
    scan_date = None
    json_mode = False
    signal_filter = None
    channel_id = None
    pead_days = 5
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg in ('--help', '-h'):
            print(__doc__.strip())
            return
        if arg == '--date' and i + 1 < len(args):
            scan_date = args[i + 1]
        if arg == '--json':
            json_mode = True
        if arg == '--signal' and i + 1 < len(args):
            signal_filter = args[i + 1].upper()
        if arg == '--channel' and i + 1 < len(args):
            channel_id = args[i + 1]
        if arg == '--pead-days':
            if i + 1 >= len(args):
                print('ERROR: --pead-days は 1 以上の整数で指定してください', file=sys.stderr)
                sys.exit(2)
            try:
                pead_days = int(args[i + 1])
            except ValueError:
                print('ERROR: --pead-days は 1 以上の整数で指定してください', file=sys.stderr)
                sys.exit(2)

    if signal_filter and signal_filter not in SIGNAL_LABELS:
        print('ERROR: --signal は A/B/C/D/E/F/G のいずれかで指定してください', file=sys.stderr)
        sys.exit(2)

    if signal_filter == 'F':
        run_media_alpha(json_mode=json_mode, channel_id=channel_id)
        return

    if scan_date and not re.fullmatch(r'\d{8}', scan_date):
        print('ERROR: --date は YYYYMMDD 形式で指定してください', file=sys.stderr)
        sys.exit(2)
    if pead_days < 1:
        print('ERROR: --pead-days は 1 以上の整数で指定してください', file=sys.stderr)
        sys.exit(2)

    print('直近取引日を確認中...', file=sys.stderr)
    scan_date = scan_date or last_trading_date()
    print(f'スキャン日: {scan_date}', file=sys.stderr)
    skip_pead = signal_filter in ('A', 'B', 'C', 'D', 'G')
    skip_margin = signal_filter in ('A', 'B', 'C', 'D', 'E')

    # Signal E (PEAD) は最初に取得する。
    # PEADは件数が多く時間がかかるため、後段のA-D/G取得に埋もれさせない。
    # 以前は120秒超過時に結果を捨てていたが、検証母集団を欠落させるためタイムアウトしない。
    if skip_pead:
        print(f'Signal E スキップ (--signal {signal_filter} 指定)', file=sys.stderr)
        pead_signals = []
    else:
        pead_dates = prev_biz_days(pead_days, from_date=scan_date)
        print(f'Signal E 優先取得: {",".join(pead_dates)}', file=sys.stderr)
        pead_signals = scan_pead(pead_dates)

    if signal_filter == 'E':
        disclosures = {'items': []}
        abcd_signals = []
        margin_signals = []
        signals = pead_signals
        print(f'  合計シグナル: {len(signals)} 件', file=sys.stderr)
        # A-D/Gを取らないことで、PEAD単独取得を確実かつ短時間にする。
        # 以降は通常どおりSignal E上位候補に財務データを付与して出力する。
        PER_BUCKET = 5
        top_codes = list(dict.fromkeys(s['code'] for s in signals if s['signal'] == 'E'))[:PER_BUCKET]
        financials: dict = {}
        print(f'財務データ取得中（{len(top_codes)}件）...', file=sys.stderr)
        for code in top_codes:
            fin = fetch_financials(code)
            financials[code] = fin
            roe = fin.get('roe')
            fcf = fin.get('fcf')
            print(f'  [{code}] ROE={roe}% FCF={fcf}', file=sys.stderr)

        if json_mode:
            output_json(signals, financials, disclosures, scan_date)
        else:
            report = format_report(signals, financials, scan_date, len(disclosures['items']))
            print(report)
        return

    print('TDNet 開示データ取得中...', file=sys.stderr)
    disclosures = fetch_tdnet_disclosures(scan_date)
    print(f'  {len(disclosures["items"])} 件取得', file=sys.stderr)

    today_str = date.today().strftime('%Y%m%d')
    is_historical = scan_date != today_str

    print('値下がりランキング取得中...', file=sys.stderr)
    if is_historical:
        print('  ⚠ --date が今日以外のためSignal D（値下がりランキング）をスキップ（ライブデータは日付混在になるため）', file=sys.stderr)
        losers = []
    else:
        losers = fetch_losers()
    print(f'  {len(losers)} 件取得', file=sys.stderr)

    print('シグナル分類中...', file=sys.stderr)
    abcd_signals = classify(disclosures, losers)
    print(f'  A-D シグナル検出: {len(abcd_signals)} 件', file=sys.stderr)

    # Signal G (信用需給): Signal A/B/C 上位銘柄の信用倍率をスキャン
    top_abcd_codes = list(dict.fromkeys(
        s['code'] for s in abcd_signals[:20]
    ))
    if skip_margin:
        print(f'Signal G スキップ (--signal {signal_filter} 指定)', file=sys.stderr)
        margin_signals = []
    else:
        print(f'Signal G (信用需給): {len(top_abcd_codes)} 銘柄をスキャン中...', file=sys.stderr)
        margin_signals = scan_margin_signals(top_abcd_codes)
        print(f'  Signal G 候補: {len(margin_signals)} 件', file=sys.stderr)
        # company 名を abcd_signals から補完
        code_to_company = {s['code']: s.get('company', s['code']) for s in abcd_signals}
        for ms in margin_signals:
            ms['company'] = code_to_company.get(ms['code'], ms['code'])

    signals = abcd_signals + pead_signals + margin_signals
    # Signal E 重複除去（同一銘柄の複数決算短信スキャンで重複することがある）
    seen_e: dict = {}
    deduped = []
    for s in signals:
        if s['signal'] == 'E':
            if s['code'] not in seen_e or s.get('pead_score', 0) > seen_e[s['code']].get('pead_score', 0):
                seen_e[s['code']] = s
        else:
            deduped.append(s)
    signals = deduped + list(seen_e.values())
    if signal_filter:
        signals = [s for s in signals if s['signal'] == signal_filter]
    print(f'  合計シグナル: {len(signals)} 件', file=sys.stderr)

    # 各シグナル種別から上位3件ずつ取得し財務エンリッチ（偽陰性防止）
    PER_BUCKET = 5
    bucket_codes: list[str] = []
    for sig in ['A', 'B', 'C', 'D', 'E', 'G']:
        bucket_codes += [s['code'] for s in signals if s['signal'] == sig][:PER_BUCKET]
    top_codes = list(dict.fromkeys(bucket_codes))  # 重複除去・順序維持
    financials: dict = {}
    print(f'財務データ取得中（{len(top_codes)}件）...', file=sys.stderr)
    for code in top_codes:
        fin = fetch_financials(code)
        financials[code] = fin
        roe = fin.get('roe')
        fcf = fin.get('fcf')
        print(f'  [{code}] ROE={roe}% FCF={fcf}', file=sys.stderr)

    if json_mode:
        output_json(signals, financials, disclosures, scan_date)
    else:
        report = format_report(signals, financials, scan_date, len(disclosures['items']))
        print(report)

        # 結果保存
        results_dir = SKILL_DIR / 'results'
        results_dir.mkdir(exist_ok=True)
        out_path = results_dir / f'{date.today().isoformat()}.md'
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(f'# 歪みスキャン {date.today().isoformat()}\n\n```\n{report}\n```\n')
        print(f'保存: {out_path}', file=sys.stderr)


if __name__ == '__main__':
    main()
