#!/usr/bin/env python3
"""
market-distortion-scan / filter.py
適時開示・大量保有データを受け取り、3種の歪みシグナルを分類・スコアリングする。

Usage:
  python3 filter.py <disclosures.json> <holdings.json>

Output:
  JSON array of detected signals, sorted by score desc.
  [{"signal": "A"|"B"|"C", "code": "1234", "company": "...",
    "title": "...", "score": 0-100, "reason": "..."}]
"""
import json
import sys
import unicodedata


def normalize(text: str) -> str:
    """全角英数字・記号を半角に正規化"""
    return unicodedata.normalize('NFKC', text)

# ------------------------------------------------------------------ helpers --

INSTITUTIONAL_KEYWORDS = [
    '株式会社', '有限会社', '合同会社', 'ファンド', 'キャピタル', 'アセット',
    'マネジメント', 'インベスト', 'パートナーズ', 'ホールディング', 'グループ',
    'リミテッド', 'Limited', 'Ltd', 'LLC', 'LP', 'LLP', 'Pty', 'Inc', 'Corp',
    'マネジャー', 'トラスト', '銀行', '証券', '生命', '保険', '信託', '投資',
    'フィナンシャル', 'Management', 'Capital', 'Asset', 'Fund', 'Partners',
    'Investments', 'Advisors', 'エクイティ', 'バリュー', 'アクティビスト',
]

# 既知のアクティビスト系（高スコア加点）
ACTIVIST_KEYWORDS = [
    'Oasis', 'ストラテジックキャピタル', 'カナメ', 'エリオット',
    'シティインデックス', 'アプリカス', 'ValueAct',
]


def is_institutional(name: str) -> bool:
    n = normalize(name)
    return any(kw in n for kw in INSTITUTIONAL_KEYWORDS)


def is_activist(name: str) -> bool:
    n = normalize(name)
    return any(kw in n for kw in ACTIVIST_KEYWORDS)


# ----------------------------------------------------------------- Signal A --

def score_signal_a(item: dict) -> tuple[int | None, str | None]:
    """業績上方修正シグナル: タイトルのキーワード強度でスコア付け"""
    title = item.get('title', '')

    if '下方修正' in title:
        return None, None

    if '大幅上方修正' in title:
        return 90, '大幅上方修正'

    if '上方修正' in title and ('業績予想' in title or '業績' in title):
        return 75, '業績予想上方修正'

    if '上方修正' in title:
        return 65, '上方修正'

    # 「業績修正」単独（訂正・下方でないもの）
    if '業績修正' in title and '訂正' not in title:
        return 60, '業績予想修正'

    return None, None


# ----------------------------------------------------------------- Signal B --

def score_signal_b(ev: dict) -> tuple[int | None, str | None]:
    """大量保有出現シグナル: 機関投資家の新規・増加取得"""
    holder = ev.get('holderName', '')
    ratio = ev.get('holdingRatio') or 0
    prev = ev.get('previousRatio')

    # 5〜15%のみ対象（過半数保有は市場歪みではない）
    if not (5 <= ratio <= 15):
        return None, None

    # 機関投資家のみ
    if not is_institutional(holder):
        return None, None

    # 保有増加または新規のみ（減少は除外）
    is_new = prev is None or prev == 0
    is_increase = prev is not None and ratio > prev

    if not is_new and not is_increase:
        return None, None

    score = 80 if is_new else 60

    # アクティビスト加点
    if is_activist(holder):
        score = min(score + 10, 100)

    if is_new:
        reason = f'新規取得 {ratio:.2f}%'
    else:
        reason = f'保有増加 {prev:.1f}%→{ratio:.2f}%'

    return score, reason


# ----------------------------------------------------------------- Signal C --

def score_signal_c(item: dict) -> tuple[int | None, str | None]:
    """PBR割れ自社株買いシグナル: 自社株取得決議"""
    title = item.get('title', '')

    # 取得状況（進捗報告）・消却は除外
    if '取得状況' in title or '消却' in title or '処分' in title:
        return None, None

    if '自己株式の取得' in title or '自己の株式の取得' in title:
        return 70, '自社株買い取得決議'

    if '自己株式取得' in title:
        return 70, '自社株買い取得決議'

    if '自社株' in title and '取得' in title:
        return 65, '自社株買い'

    return None, None


# ----------------------------------------------------------------- Signal D --

BAD_KEYWORDS = [
    '下方修正', '業績悪化', '不正', '調査', '損失', '引当', '減損', '訂正', '行政処分',
]


def score_signal_d(loser: dict, disclosures_by_code: dict) -> tuple[int | None, str | None]:
    """暴落過剰反応シグナル: 悪材料なし -8%以上の急落銘柄"""
    code = loser['code']
    change_pct = loser['change_pct']

    # 悪材料開示があれば除外
    related = disclosures_by_code.get(code, [])
    for item in related:
        title = item.get('title', '')
        if any(kw in title for kw in BAD_KEYWORDS):
            return None, None

    # 変化率でベーススコア
    if change_pct <= -20:
        score = 90
    elif change_pct <= -15:
        score = 80
    else:
        score = 70

    # 開示なし（悪材料確認済み）で加点
    if not related:
        score = min(score + 10, 100)
        reason = f'前日比{change_pct:.1f}% / 開示なし'
    else:
        reason = f'前日比{change_pct:.1f}% / 悪材料なし'

    return score, reason


def process_losers(losers: list, disclosures: dict) -> list[dict]:
    # 銘柄コードで開示をインデックス化
    disclosures_by_code: dict[str, list] = {}
    for item in disclosures.get('items', []):
        code = item.get('code', '')
        disclosures_by_code.setdefault(code, []).append(item)

    results = []
    for loser in losers:
        score, reason = score_signal_d(loser, disclosures_by_code)
        if score is None:
            continue
        results.append({
            'signal': 'D',
            'code': loser['code'],
            'company': loser['name'],
            'title': f'前日比{loser["change_pct"]:.1f}% / 株価{loser["price"]:,.0f}円',
            'score': score,
            'reason': reason,
            'change_pct': loser['change_pct'],
        })
    return results


# ---------------------------------------------------------- dedup & process --

def process_disclosures(data: dict) -> list[dict]:
    results = []
    for item in data.get('items', []):
        code = item.get('code', '')
        company = item.get('companyName', '')
        title = item.get('title', '')

        score_a, reason_a = score_signal_a(item)
        if score_a is not None:
            results.append({
                'signal': 'A', 'code': code, 'company': company,
                'title': title, 'score': score_a, 'reason': reason_a,
            })

        score_c, reason_c = score_signal_c(item)
        if score_c is not None:
            results.append({
                'signal': 'C', 'code': code, 'company': company,
                'title': title, 'score': score_c, 'reason': reason_c,
            })

    return results


def process_holdings(data: dict) -> list[dict]:
    results = []
    for ev in data.get('events', []):
        score, reason = score_signal_b(ev)
        if score is None:
            continue
        code = ev.get('secCode', '')
        company = ev.get('issuerName', '')
        holder = ev.get('holderName', '').replace('\n', ' ')
        results.append({
            'signal': 'B', 'code': code, 'company': company,
            'title': f'{holder}が{ev.get("holdingRatio", 0):.2f}%保有',
            'score': score, 'reason': reason,
        })
    return results


def dedup(results: list[dict]) -> list[dict]:
    """同一銘柄・同一シグナルは高スコアのみ残す"""
    seen: dict[tuple, dict] = {}
    for r in sorted(results, key=lambda x: x['score'], reverse=True):
        key = (r['signal'], r['code'])
        if key not in seen:
            seen[key] = r
    return list(seen.values())


# ----------------------------------------------------------------------- main --

def main():
    if len(sys.argv) < 3:
        print('Usage: filter.py <disclosures.json> <holdings.json> [losers.json]', file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1], encoding='utf-8') as f:
        disclosures = json.load(f)
    with open(sys.argv[2], encoding='utf-8') as f:
        holdings = json.load(f)

    results = []
    results.extend(process_disclosures(disclosures))
    results.extend(process_holdings(holdings))

    if len(sys.argv) >= 4:
        with open(sys.argv[3], encoding='utf-8') as f:
            losers = json.load(f)
        results.extend(process_losers(losers, disclosures))

    results = dedup(results)
    results.sort(key=lambda x: x['score'], reverse=True)

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
