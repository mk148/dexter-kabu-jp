#!/usr/bin/env python3
"""
株主優待・配当利回り一括取得スクリプト
kabutan.jp の優待ページから配当利回り・優待利回り・合計利回りを取得する
"""

import requests
from bs4 import BeautifulSoup
import csv
import time
import re
import sys
import json
import warnings
warnings.filterwarnings('ignore')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Accept-Language': 'ja,en-US;q=0.9',
}

KABUYUTAI_API = "https://www.kabuyutai.com/tool/api/search/"

def parse_pct(text):
    m = re.search(r'([\d.]+)\s*%', text)
    if m:
        v = float(m.group(1))
        return v if v < 100 else None
    return None

def parse_float(value):
    try:
        if value is None or value == '':
            return 0.0
        return float(str(value).replace(',', ''))
    except ValueError:
        return 0.0

def extract_yutai_value_from_text(text, min_shares, price):
    """ページテキストから優待価値(円)を抽出して年間利回りを計算。－%の場合のフォールバック。

    優先度:
      1. 年間X円相当 / 年間X円分  → 年間価値として採用
      2. X円相当 / X円分 / QUOカードXX円 等 → 単期価値として採用
      3. X枚の買物優待券 (1枚=100円) → 枚×100円で換算
      4. Xポイント (1ポイント=1円) → ポイント数を円換算
    """
    if min_shares <= 0 or price <= 0:
        return 0.0

    invest = min_shares * price

    # --- 1. 年間表示を最優先で探す ---
    annual_amounts = []
    for pat in [r'年間\s*([\d,]+)円相当', r'年間\s*([\d,]+)円分', r'年間\s*([\d,]+)円']:
        for m in re.finditer(pat, text):
            v = int(m.group(1).replace(',', ''))
            if 500 <= v <= 200000:
                annual_amounts.append(v)
    if annual_amounts:
        return round(min(annual_amounts) / invest * 100, 2)

    # --- 2. 円金額パターン (半期分など) ---
    amounts = []
    for pat in [
        r'([\d,]+)円相当', r'([\d,]+)円分',
        r'([\d,]+)円のQUO', r'QUO[カ-ン]*\s*([\d,]+)円',
        r'([\d,]+)円のギフト', r'([\d,]+)円のデジタル',
    ]:
        for m in re.finditer(pat, text):
            v = int(m.group(1).replace(',', ''))
            if 500 <= v <= 200000:
                amounts.append(v)
    if amounts:
        return round(min(amounts) / invest * 100, 2)

    # --- 3. 買物優待券 X枚 → 1枚=100円換算 ---
    # 「年間X枚」優先、なければ単期X枚
    coupon_annual = re.search(r'年間\s*([\d,]+)枚', text)
    coupon_single = re.search(r'([\d,]+)枚', text)
    coupon_match  = coupon_annual or coupon_single
    if coupon_match and any(k in text for k in ['買物優待券', '優待券', 'イオン優待']):
        count = int(coupon_match.group(1).replace(',', ''))
        if 1 <= count <= 500:
            coupon_value = count * 100  # 1枚=100円
            if 500 <= coupon_value <= 200000:
                return round(coupon_value / invest * 100, 2)

    # --- 4. ポイント → 1ポイント=1円換算（re.finditerで全マッチを探索）---
    for pat in [r'年間\s*([\d,]+)ポイント', r'([\d,]+)ポイント']:
        for m in re.finditer(pat, text):
            pts = int(m.group(1).replace(',', ''))
            if 500 <= pts <= 50000:
                return round(pts / invest * 100, 2)

    return 0.0

def fetch_stock(code, name, price, holdings):
    result = {
        'code': code, 'name': name, 'price': price, 'holdings': holdings,
        'total_yield': 0.0, 'yutai_yield': 0.0, 'div_yield': 0.0,
        'yutai_note': '', 'status': 'ok'
    }
    try:
        url = f"https://kabutan.jp/stock/yutai?code={code}"
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.encoding = 'utf-8'
        soup = BeautifulSoup(r.text, 'html.parser')
        rows = soup.find_all('tr')
        full_text = soup.get_text()

        # 最低必要株数を取得
        min_shares_match = re.search(r'最低必要株数\s*([\d,]+)株', full_text)
        min_shares = int(min_shares_match.group(1).replace(',','')) if min_shares_match else holdings

        for i, tr in enumerate(rows):
            headers_text = [td.get_text(strip=True) for td in tr.find_all(['td','th'])]
            joined = ' '.join(headers_text)

            if '【優待＋配当】利回り' in joined and i + 1 < len(rows):
                vals = [td.get_text(strip=True) for td in rows[i+1].find_all(['td','th'])]
                if len(vals) >= 3:
                    result['total_yield'] = parse_pct(vals[0]) or 0.0
                    result['div_yield']   = parse_pct(vals[2]) or 0.0
                    yutai_raw = parse_pct(vals[1])
                    if yutai_raw is not None and yutai_raw > 0:
                        result['yutai_yield'] = yutai_raw
                    else:
                        # 「－%」または「0%」の場合：テキストから優待価値を計算
                        result['yutai_yield'] = extract_yutai_value_from_text(full_text, min_shares, price)
                        if result['yutai_yield'] > 0:
                            result['total_yield'] = result['div_yield'] + result['yutai_yield']
                            result['status'] = 'ok(calc)'
                elif len(vals) == 2:
                    result['total_yield'] = parse_pct(vals[0]) or 0.0
                    result['div_yield']   = parse_pct(vals[1]) or 0.0
                break

            if '配当利回り' in joined and '優待' not in joined:
                for td in tr.find_all(['td','th'])[1:]:
                    v = parse_pct(td.get_text())
                    if v is not None:
                        result['div_yield'] = v
                        result['total_yield'] = v
                        break

        for tr in rows:
            cells = [td.get_text(strip=True) for td in tr.find_all(['td','th'])]
            joined = ' '.join(cells)
            if '優待内容:' in joined or '優待内容' in joined:
                content = ' '.join(c for c in cells if c and '優待内容' not in c)
                result['yutai_note'] = content[:30]
                break

        if result['total_yield'] == 0.0 and result['div_yield'] == 0.0:
            result['status'] = 'no_data'

    except Exception as e:
        result['status'] = f'err:{str(e)[:30]}'

    return result

def fetch_kabuyutai_stock(code, name, price, holdings, fallback_result=None):
    result = fallback_result or {
        'code': code, 'name': name, 'price': price, 'holdings': holdings,
        'total_yield': 0.0, 'yutai_yield': 0.0, 'div_yield': 0.0,
        'yutai_note': '', 'status': 'no_data'
    }
    try:
        r = requests.get(
            KABUYUTAI_API,
            params={'kw': code, 'p': 1},
            headers={**HEADERS, 'Referer': 'https://www.kabuyutai.com/tool/shiborikomi/'},
            timeout=15,
            verify=False,
        )
        data = r.json()
        matches = [item for item in data.get('result', []) if str(item.get('code')) == code]
        if not matches:
            return result

        item = matches[0]
        result['name'] = item.get('company') or name
        result['total_yield'] = parse_float(item.get('totalYield'))
        result['yutai_yield'] = parse_float(item.get('preferentialYield'))
        result['div_yield'] = parse_float(item.get('dividendYield'))
        result['yutai_note'] = re.sub(r'<.*?>', '', item.get('detail') or '')[:30]
        result['status'] = 'ok(kabuyutai)'
    except Exception as e:
        if result['status'] == 'no_data':
            result['status'] = f'err:kabuyutai:{str(e)[:20]}'
    return result


def main():
    # JSONから銘柄リストを読み込む
    if len(sys.argv) < 2:
        print("Usage: python fetch_yield.py stocks.json [output.csv]", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1], encoding='utf-8') as f:
        stocks = json.load(f)

    output_csv = sys.argv[2] if len(sys.argv) > 2 else 'yield_result.csv'

    total = len(stocks)
    results = []

    print(f"取得開始 ({total}銘柄) ...", flush=True)

    for i, s in enumerate(stocks, 1):
        code     = str(s['code'])
        name     = s.get('name', code)
        price    = float(s.get('price', 0))
        holdings = int(s.get('holdings', 100))

        r = fetch_stock(code, name, price, holdings)
        if r['status'] not in ('ok', 'ok(calc)'):
            r = fetch_kabuyutai_stock(code, name, price, holdings, r)
        results.append(r)

        status = f"合計{r['total_yield']:.2f}%" if r['total_yield'] > 0 else r['status']
        print(f"[{i:3}/{total}] {code} {name[:18]:<18} {status}", flush=True)
        time.sleep(0.8)

    results.sort(key=lambda x: x['total_yield'], reverse=True)

    with open(output_csv, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'code','name','price','holdings',
            'total_yield','div_yield','yutai_yield','yutai_note','status'
        ])
        writer.writeheader()
        writer.writerows(results)

    print(f"\n{'='*78}")
    print(f"{'コード':<6} {'社名':<22} {'株価':>6} {'配当%':>6} {'優待%':>6} {'合計%':>6}  優待内容")
    print(f"{'-'*78}")
    for r in results:
        print(f"{r['code']:<6} {r['name']:<22} {r['price']:>6.0f} "
              f"{r['div_yield']:>5.2f}% {r['yutai_yield']:>5.2f}% "
              f"{r['total_yield']:>5.2f}%  {r['yutai_note'][:18]}")

    no_data = [r for r in results if r['status'] not in ('ok', 'ok(calc)', 'ok(kabuyutai)')]
    print(f"\nCSV保存: {output_csv}")
    if no_data:
        print(f"データなし/エラー ({len(no_data)}件): {', '.join(r['code'] for r in no_data)}")


if __name__ == '__main__':
    main()
