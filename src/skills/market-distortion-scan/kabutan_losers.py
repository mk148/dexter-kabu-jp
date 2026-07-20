#!/usr/bin/env python3
"""
kabutan.jp から東証プライム値下がり率ランキングを取得し /tmp/kabu_losers.json に保存する。

Usage:
  python3 kabutan_losers.py [--threshold -8.0]

Output:
  /tmp/kabu_losers.json
  [{"code": "1234", "name": "〇〇工業", "change_pct": -9.2, "price": 850}, ...]
"""
import json
import re
import ssl
import sys
import urllib.request

# 値下がり率ランキング（東証プライム） URL
RANKING_URL = 'https://kabutan.jp/warning/?mode=2_2'
OUTPUT_PATH = '/tmp/kabu_losers.json'
DEFAULT_THRESHOLD = -8.0

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def fetch_losers(threshold: float = DEFAULT_THRESHOLD) -> list[dict]:
    req = urllib.request.Request(
        RANKING_URL,
        headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
    )
    with urllib.request.urlopen(req, context=ctx) as r:
        html = r.read().decode('utf-8', errors='ignore')

    # ページ内の3番目の<table>が株価ランキングテーブル
    tables = re.findall(r'<table[^>]*>(.*?)</table>', html, re.DOTALL)
    if len(tables) < 3:
        raise RuntimeError(f'ランキングテーブルが見つかりません (tables={len(tables)})')

    ranking_table = tables[2]
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', ranking_table, re.DOTALL)

    results = []
    for row in rows[1:]:  # ヘッダー行をスキップ
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)
        text = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

        # 4桁数字の銘柄コード
        code_match = re.search(r'/stock/\?code=(\d{4})', row)
        if not code_match:
            continue
        code = code_match.group(1)

        # 銘柄名（全角スペース等を除去）
        name = re.sub(r'\s+', '', text[1]) if len(text) > 1 else ''

        # 株価（カンマ除去して数値化）
        price = 0.0
        for t in text[4:7]:
            t_clean = t.replace(',', '')
            if re.match(r'^\d+(\.\d+)?$', t_clean):
                price = float(t_clean)
                break

        # 前日比(%)
        change_pct = None
        for t in text:
            m = re.match(r'^([+-]\d+\.\d+)%$', t)
            if m:
                change_pct = float(m.group(1))
                break

        if change_pct is None or change_pct > threshold:
            continue

        # 出来高 0 の場合は市場閉鎖日（土日祝）→ スキップ
        volume = 0
        for t in text:
            t_clean = t.replace(',', '')
            if re.match(r'^\d{4,}$', t_clean):
                volume = int(t_clean)
                break
        if volume == 0:
            continue

        results.append({
            'code': code,
            'name': name,
            'change_pct': change_pct,
            'price': price,
        })

    return results


def main():
    threshold = DEFAULT_THRESHOLD
    for i, arg in enumerate(sys.argv[1:]):
        if arg == '--threshold' and i + 2 < len(sys.argv):
            threshold = float(sys.argv[i + 2])

    losers = fetch_losers(threshold)

    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(losers, f, ensure_ascii=False, indent=2)

    print(f'{len(losers)}件の暴落銘柄を {OUTPUT_PATH} に保存しました', file=sys.stderr)
    for r in losers:
        print(f"  {r['code']} {r['name']} {r['change_pct']:+.1f}% ¥{r['price']:,.0f}", file=sys.stderr)


if __name__ == '__main__':
    main()
