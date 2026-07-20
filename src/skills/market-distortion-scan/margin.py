#!/usr/bin/env python3
"""
Signal G: 信用需給シグナル — 信用倍率急増・需給ひっ迫検出モジュール。

kabutan.jp の信用残ページから信用倍率を取得し、
倍率 >= 3.0 または前週比 +50% 以上の銘柄を Signal G として返す。
"""
import re
import ssl
import sys
import time
import urllib.request

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}


def get(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='replace')


def fetch_margin_ratio(code: str) -> dict:
    """
    kabutan.jp の信用残ページから信用倍率・買い残・売り残を取得する。

    Returns:
        {
            'margin_ratio': float,       # 信用倍率（買い残/売り残）
            'buy_balance': float,        # 買い残（千株）
            'sell_balance': float,       # 売り残（千株）
            'prev_margin_ratio': float,  # 前週の信用倍率（取得できる場合）
        }
        取得失敗時は空 dict を返す。
    """
    result: dict = {}
    try:
        html = get(f'https://kabutan.jp/stock/margin?code={code}')

        # 信用倍率テーブルを探す
        # <td class="...">3.45</td> のような形式で倍率が入っている
        # kabutan の信用残テーブル: 買い残・売り残・信用倍率の順
        tables = re.findall(r'<table[^>]*>(.*?)</table>', html, re.DOTALL)

        for table in tables:
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table, re.DOTALL)
            for i, row in enumerate(rows):
                cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)
                text = [re.sub(r'<[^>]+>', '', c).strip().replace(',', '') for c in cells]

                # 信用倍率行を探す（「倍」という単位または「信用倍率」というヘッダー）
                if any('倍率' in t or '信用倍率' in t for t in text):
                    # 数値を抽出
                    nums = []
                    for t in text:
                        try:
                            nums.append(float(t))
                        except ValueError:
                            nums.append(None)
                    floats = [n for n in nums if n is not None and n > 0]
                    if floats:
                        result['margin_ratio'] = floats[0]

                # 買い残・売り残・倍率の行を探す（kabutan の典型的なレイアウト）
                # 行ヘッダーが「信用残」「融資残高」等を含む
                if len(text) >= 4:
                    nums = []
                    for t in text:
                        t_clean = t.replace(',', '').replace('倍', '')
                        try:
                            nums.append(float(t_clean))
                        except ValueError:
                            nums.append(None)
                    valid = [n for n in nums if n is not None]
                    # 最後の数値が信用倍率の可能性が高い（買い残/売り残より小さい数）
                    if len(valid) >= 3 and 'margin_ratio' not in result:
                        # 倍率は通常 0.1〜100 の範囲
                        candidates = [v for v in valid if 0.1 <= v <= 100]
                        if candidates:
                            result['margin_ratio'] = candidates[-1]

        # 直接 margin_ratio を探す（別のパターン）
        if 'margin_ratio' not in result:
            # 「XX倍」パターン
            m = re.search(r'信用倍率[^<]*?</t[dh]>\s*<td[^>]*>([\d.]+)', html)
            if m:
                try:
                    result['margin_ratio'] = float(m.group(1))
                except ValueError:
                    pass

        # 信用残テーブルから買い残・売り残・倍率を直接パース
        # パターン: class="margin" 等のテーブル
        margin_section = re.search(
            r'(?:買い残|融資残高|信用買残).*?</table>',
            html, re.DOTALL
        )
        if margin_section:
            section = margin_section.group(0)
            # 数値テーブルの行から最新2行（今週・先週）を取得
            data_rows = []
            for row in re.findall(r'<tr[^>]*>(.*?)</tr>', section, re.DOTALL):
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                text = [re.sub(r'<[^>]+>', '', c).strip().replace(',', '') for c in cells]
                nums = []
                for t in text:
                    try:
                        nums.append(float(t))
                    except ValueError:
                        nums.append(None)
                valid = [n for n in nums if n is not None]
                if len(valid) >= 3:
                    data_rows.append(valid)

            if len(data_rows) >= 1:
                row0 = data_rows[0]
                # 買い残[0], 売り残[1], 倍率[2] と仮定
                if len(row0) >= 3:
                    result['buy_balance'] = row0[0]
                    result['sell_balance'] = row0[1]
                    result['margin_ratio'] = row0[2]

            if len(data_rows) >= 2:
                row1 = data_rows[1]
                if len(row1) >= 3:
                    result['prev_margin_ratio'] = row1[2]

    except Exception:
        pass

    return result


def scan_margin_signals(codes: list) -> list:
    """
    信用倍率急増銘柄を検出して Signal G として返す。

    条件:
      - 信用倍率 >= 3.0 (買い圧力の高まり)
      OR
      - 前週比 +50% 以上の倍率上昇

    Args:
        codes: 4桁銘柄コードのリスト

    Returns:
        Signal G のリスト [{signal, code, company, score, reason, margin_ratio, ...}]
    """
    results = []
    for code in codes:
        try:
            data = fetch_margin_ratio(code)
        except Exception:
            continue

        ratio = data.get('margin_ratio')
        prev_ratio = data.get('prev_margin_ratio')

        if ratio is None:
            continue

        score = None
        reason_parts = []

        # 条件1: 倍率 >= 3.0
        if ratio >= 5.0:
            score = 80
            reason_parts.append(f'信用倍率{ratio:.1f}倍（高水準）')
        elif ratio >= 3.0:
            score = 70
            reason_parts.append(f'信用倍率{ratio:.1f}倍')

        # 条件2: 前週比 +50% 以上
        if prev_ratio and prev_ratio > 0:
            change_pct = (ratio - prev_ratio) / prev_ratio * 100
            if change_pct >= 100:
                score = max(score or 0, 85)
                reason_parts.append(f'前週比+{change_pct:.0f}%急増')
            elif change_pct >= 50:
                score = max(score or 0, 75)
                reason_parts.append(f'前週比+{change_pct:.0f}%増加')

        if score is None:
            continue

        results.append({
            'signal': 'G',
            'code': code,
            'company': code,  # 呼び出し元で補完する
            'title': f'信用倍率{ratio:.1f}倍',
            'score': score,
            'reason': ' / '.join(reason_parts),
            'margin_ratio': ratio,
            'prev_margin_ratio': prev_ratio,
        })

        time.sleep(0.3)

    results.sort(key=lambda x: x['score'], reverse=True)
    return results


if __name__ == '__main__':
    # 単体テスト: いくつかの銘柄で信用倍率を確認
    test_codes = ['7203', '9984', '6758', '4661', '2413']
    print('信用倍率スキャン テスト実行')
    print(f'対象: {test_codes}')
    results = scan_margin_signals(test_codes)
    if results:
        print(f'\nSignal G 候補: {len(results)} 件')
        for r in results:
            print(f"  ● {r['code']} — {r['reason']} (score={r['score']})")
    else:
        print('\nSignal G なし（閾値未達）')

    # 個別取得テスト
    print('\n--- 個別取得テスト ---')
    for code in test_codes[:3]:
        data = fetch_margin_ratio(code)
        print(f'  {code}: {data}')
