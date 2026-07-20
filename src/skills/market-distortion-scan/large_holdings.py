#!/usr/bin/env python3
"""
market-distortion-scan / large_holdings.py
TDNetの大量保有報告書からSignal B（機関投資家新規取得）候補を抽出する。

Usage:
    python3 large_holdings.py [--date YYYYMMDD]

Output:
    JSON array of Signal B candidates.

Note:
    このモジュールは run_scan.py の classify() 内で自動的に処理される。
    単独実行は確認・デバッグ用。
"""
import json
import re
import ssl
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}

INSTITUTIONAL_KEYWORDS = [
    'ファンド', 'キャピタル', 'アセット', 'マネジメント', 'インベスト', 'パートナーズ',
    '証券', '銀行', '生命', '保険', '信託', '投資', 'フィナンシャル',
    'Management', 'Capital', 'Asset', 'Fund', 'Partners', 'Investments',
    'Advisors', 'Limited', 'Ltd', 'LLC', 'LP',
]

# アクティビスト投資家リスト（日本市場での提案実績あり）
# score_boost: スコア加算値、 track: 主な活動実績
ACTIVIST_INVESTORS: dict[str, dict] = {
    'Oasis': {'score_boost': 25, 'track': '株主還元・ガバナンス改善要求'},
    'ストラテジックキャピタル': {'score_boost': 25, 'track': '非中核事業売却・ROE改善要求'},
    'カナメキャピタル': {'score_boost': 20, 'track': '自己株取得・配当増要求'},
    'エリオット': {'score_boost': 25, 'track': '大型グローバルアクティビスト'},
    'シティインデックスイレブンス': {'score_boost': 20, 'track': '中小型株 株主還元要求'},
    'アプリカス': {'score_boost': 20, 'track': '少数株主保護・ガバナンス'},
    'ValueAct': {'score_boost': 25, 'track': 'グローバルアクティビスト'},
    'Effissimo': {'score_boost': 20, 'track': '東芝・その他大型案件'},
    'エフィッシモ': {'score_boost': 20, 'track': '東芝・その他大型案件'},
    'レノ': {'score_boost': 20, 'track': 'ストラテジックキャピタル関連'},
    'リム': {'score_boost': 15, 'track': '中小型株アクティビスト'},
    '村上': {'score_boost': 20, 'track': '村上ファンド系'},
    'C&I': {'score_boost': 15, 'track': '中小型株 株主還元'},
    'アントラーズ': {'score_boost': 15, 'track': '小型株アクティビスト'},
    'Dalton': {'score_boost': 20, 'track': 'ガバナンス改善要求'},
    'ダルトン': {'score_boost': 20, 'track': 'ガバナンス改善要求'},
    'Pelham': {'score_boost': 15, 'track': 'グローバルアクティビスト'},
    'Third Point': {'score_boost': 20, 'track': 'グローバルアクティビスト'},
    'Starboard': {'score_boost': 20, 'track': 'グローバルアクティビスト'},
}

# パッシブ系大手（スコア加算なし、フィルタリング用）
PASSIVE_LARGE_INVESTORS = [
    'ブラックロック', 'BlackRock', 'バンガード', 'Vanguard',
    'ステートストリート', 'State Street', '日本生命', '第一生命',
    'かんぽ生命', '農林中央金庫', 'ゆうちょ銀行', '年金積立金',
    'GPIF', 'Norges', 'ノルジーズ',
]

# 以前からアクティビストキーワードとして残す（後方互換性）
ACTIVIST_KEYWORDS = list(ACTIVIST_INVESTORS.keys())


_FILER_CACHE: dict[str, str] = {}  # tdnet_url -> filer name


def get(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='replace')


def fetch_filer_name(tdnet_url: str) -> str:
    """TDNet開示ページから提出者名（大量保有者名）を取得する。"""
    if not tdnet_url:
        return ''
    if tdnet_url in _FILER_CACHE:
        return _FILER_CACHE[tdnet_url]

    try:
        html = get(tdnet_url, timeout=8)
        # TDNet開示詳細ページ: 「提出者名」または「報告義務発生者」フィールドを探す
        patterns = [
            r'提出者名[^<]*</[^>]+>[^<]*<[^>]+>([^<]{2,60})',
            r'報告義務発生者[^<]*</[^>]+>[^<]*<[^>]+>([^<]{2,60})',
            r'<td[^>]*>提出者</td>\s*<td[^>]*>([^<]{2,60})',
            r'filerName["\s:]+([^\s"<,]{2,60})',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.DOTALL)
            if m:
                name = re.sub(r'\s+', ' ', m.group(1)).strip()
                if name:
                    _FILER_CACHE[tdnet_url] = name
                    return name
    except Exception:
        pass

    _FILER_CACHE[tdnet_url] = ''
    return ''


def classify_investor(filer_name: str) -> tuple[str, int, str]:
    """
    投資家タイプを判定。
    Returns: (investor_type, score_boost, activist_track)
        investor_type: 'activist' | 'passive_large' | 'institutional' | 'unknown'
    """
    if not filer_name:
        return 'unknown', 0, ''

    # アクティビスト判定
    for keyword, info in ACTIVIST_INVESTORS.items():
        if keyword.lower() in filer_name.lower():
            return 'activist', info['score_boost'], info['track']

    # パッシブ大手判定
    for kw in PASSIVE_LARGE_INVESTORS:
        if kw.lower() in filer_name.lower():
            return 'passive_large', 0, ''

    # 一般機関投資家
    for kw in INSTITUTIONAL_KEYWORDS:
        if kw in filer_name:
            return 'institutional', 5, ''

    return 'unknown', 0, ''


def extract_from_tdnet(disclosures: dict, fetch_filer: bool = False) -> list[dict]:
    """TDNetの開示リストから大量保有報告書を抽出してSignal B候補を生成する。

    Args:
        disclosures: TDNet開示アイテムリスト({'items': [...]})
        fetch_filer: Trueの場合、各開示ページにアクセスして保有者名を取得する
                     （追加ネットワークリクエストが発生。1件あたり約1秒）
    """
    results = []
    seen_codes: set = set()

    for item in disclosures.get('items', []):
        title = item.get('title', '')
        code = item.get('code', '')

        if not code:
            continue

        is_new = '大量保有報告書' in title and '変更' not in title
        is_change = '大量保有' in title and '変更報告書' in title

        if not (is_new or is_change):
            continue

        # 同一銘柄は高スコアのみ残す（後で dedup されるが念のため）
        key = (code, 'new' if is_new else 'change')
        if key in seen_codes:
            continue
        seen_codes.add(key)

        base_score = 75 if is_new else 60
        base_reason = '機関投資家新規大量保有' if is_new else '大量保有変更（増加確認要）'

        # 保有者名の取得とアクティビスト判定
        filer_name = ''
        investor_type = 'unknown'
        activist_track = ''
        score_boost = 0

        if fetch_filer:
            tdnet_url = item.get('tdnet_url', '')
            if tdnet_url:
                filer_name = fetch_filer_name(tdnet_url)
                investor_type, score_boost, activist_track = classify_investor(filer_name)
                time.sleep(0.5)  # レート制限

        score = base_score + score_boost
        reason = base_reason

        if investor_type == 'activist' and filer_name:
            reason = f'アクティビスト新規取得: {filer_name}（{activist_track}）' if is_new else f'アクティビスト保有増加: {filer_name}（{activist_track}）'
        elif investor_type == 'passive_large':
            reason = f'パッシブ大手: {filer_name} — {base_reason}'
        elif filer_name:
            reason = f'{base_reason} ({filer_name})'

        results.append({
            'signal': 'B',
            'code': code,
            'company': item.get('companyName', ''),
            'title': title,
            'score': score,
            'reason': reason,
            'investor_type': investor_type,
            'filer_name': filer_name,
            'activist_track': activist_track,
            'tdnet_url': item.get('tdnet_url', ''),
            'time': item.get('time', ''),
        })

    return results


def fetch_recent_large_holdings_from_tdnet(
    date_str: str | None = None,
    fetch_filer: bool = False,
) -> list[dict]:
    """TDNetから指定日の大量保有報告書を取得する（単独実行用）。"""
    if date_str is None:
        d = date.today()
        for _ in range(10):
            if d.weekday() < 5:
                date_str = d.strftime('%Y%m%d')
                break
            d -= timedelta(days=1)

    items = []
    for page in range(1, 15):
        try:
            html = get(f'https://www.release.tdnet.info/inbs/I_list_{page:03d}_{date_str}.html')
        except Exception:
            break

        if '開示された情報はありません' in html:
            break

        for row in re.findall(r'<tr>(.*?)</tr>', html, re.DOTALL):
            code_m = re.search(r'class="[^"]*kjCode[^"]*"[^>]*>\s*(\d{4,5})\s*', row)
            name_m = re.search(r'class="[^"]*kjName[^"]*"[^>]*>(.*?)</td>', row, re.DOTALL)
            title_m = re.search(r'class="[^"]*kjTitle[^"]*".*?<a[^>]+>([^<]+)</a>', row, re.DOTALL)
            url_m = re.search(r'class="[^"]*kjTitle[^"]*".*?<a[^>]+href="([^"]+)"', row, re.DOTALL)
            time_m = re.search(r'class="[^"]*kjTime[^"]*"[^>]*>(\d{2}:\d{2})', row)

            if not (code_m and title_m):
                continue

            title = title_m.group(1).strip()
            if '大量保有' not in title:
                continue

            href = url_m.group(1) if url_m else ''
            tdnet_url = ('https://www.release.tdnet.info' + href) if href.startswith('/') else href

            items.append({
                'code': code_m.group(1)[:4],
                'companyName': re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', name_m.group(1))).strip() if name_m else '',
                'title': title,
                'time': time_m.group(1) if time_m else '',
                'tdnet_url': tdnet_url,
            })

        total_m = re.search(r'全(\d+)件', html)
        if total_m and page * 100 >= int(total_m.group(1)):
            break
        time.sleep(0.2)

    return extract_from_tdnet({'items': items}, fetch_filer=fetch_filer)


if __name__ == '__main__':
    date_str = None
    fetch_filer = False
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == '--date' and i + 1 < len(args):
            date_str = args[i + 1]
        elif arg == '--filer':
            fetch_filer = True

    results = fetch_recent_large_holdings_from_tdnet(date_str, fetch_filer=fetch_filer)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    activist_count = sum(1 for r in results if r.get('investor_type') == 'activist')
    print(f'\n大量保有シグナル: {len(results)} 件（アクティビスト: {activist_count} 件）', file=sys.stderr)
