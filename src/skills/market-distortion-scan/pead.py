#!/usr/bin/env python3
"""
Signal E: PEAD (Post-Earnings Announcement Drift) 決算サプライズドリフト検出モジュール。

直近5営業日の決算短信をスキャンし、前年同期比・会社予想比の複合サプライズスコアと
タイトルキーワードスコアを組み合わせてドリフト候補を検出する。
"""
import re
import ssl
import sys
import time
import urllib.request
from datetime import date, timedelta

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}

POSITIVE_KEYWORDS = {
    '過去最高': 20, '最高益': 20, '黒字転換': 15,
    '大幅増益': 15, '増収増益': 12, '大幅増収': 10,
    '上方修正': 10, '好調': 8, '順調': 5,
    '連続増配': 15, '最高売上': 15, '超過達成': 10,
}
NEGATIVE_KEYWORDS = {
    '下方修正': -20, '赤字転落': -20, '大幅減益': -15,
    '減収減益': -12, '純損失': -10, '業績悪化': -10,
    '特別損失': -15, 'のれん減損': -20, '訴訟': -10,
}
EXCLUDE_PATTERNS = ['訂正', '修正申告', '補足資料', '説明会資料']
KESSAN_TITLES = ['決算短信', '四半期報告書', '業績予想']


def get(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='replace')


def prev_biz_days(n: int, from_date: str | None = None) -> list[str]:
    """直近 n 営業日（from_date 当日を含めて遡る）を YYYYMMDD 形式で返す。
    from_date が None の場合は今日を基準とする。"""
    if from_date:
        base = date(int(from_date[:4]), int(from_date[4:6]), int(from_date[6:8]))
    else:
        base = date.today()
    result = []
    d = base
    while len(result) < n:
        if d.weekday() < 5:
            result.append(d.strftime('%Y%m%d'))
        d -= timedelta(days=1)
    return result


def is_kessan_title(title: str) -> bool:
    if any(p in title for p in EXCLUDE_PATTERNS):
        return False
    return any(k in title for k in KESSAN_TITLES)


def fetch_tdnet_multi_day(date_list: list[str]) -> list[dict]:
    """複数日の TDNet から決算短信のみ取得（最大5ページ/日）"""
    all_items = []
    for date_str in date_list:
        for page in range(1, 6):
            try:
                url = f'https://www.release.tdnet.info/inbs/I_list_{page:03d}_{date_str}.html'
                html = get(url)
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
                    title = title_m.group(1).strip()
                    if not is_kessan_title(title):
                        continue
                    href = url_m.group(1) if url_m else ''
                    tdnet_url = ('https://www.release.tdnet.info' + href) if href.startswith('/') else href
                    all_items.append({
                        'code': code_m.group(1)[:4],
                        'company': re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', name_m.group(1))).strip() if name_m else '',
                        'title': title,
                        'date': date_str,
                        'time': time_m.group(1) if time_m else '',
                        'tdnet_url': tdnet_url,
                    })

            total_m = re.search(r'全(\d+)件', html)
            if total_m and page * 100 >= int(total_m.group(1)):
                break
            time.sleep(0.2)

    # 重複除去（同じコード+タイトルは一度のみ）
    seen = set()
    unique = []
    for item in all_items:
        key = (item['code'], item['title'])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def compute_text_score(title: str) -> int:
    score = 0
    for kw, pts in POSITIVE_KEYWORDS.items():
        if kw in title:
            score += pts
    for kw, pts in NEGATIVE_KEYWORDS.items():
        if kw in title:
            score += pts  # pts は負値
    return score


def parse_quarterly_yoy(html: str) -> float | None:
    """fin_quarter_result_d テーブルの「前年同期比」行 → 最終益列"""
    m = re.search(r'fin_quarter_result_d.*?</table>', html, re.DOTALL)
    if not m:
        return None
    section = m.group(0)

    yoy_m = re.search(r'前年同期比</th>(.*?)</tr>', section, re.DOTALL)
    if not yoy_m:
        return None
    row_html = yoy_m.group(1)

    # span内の数値（列順: 売上高[0], 営業益[1], 経常益[2], 最終益[3]）
    values = re.findall(r'<span[^>]*>([-+\d.]+)</span>', row_html)
    if len(values) > 3:
        try:
            return float(values[3])
        except ValueError:
            pass
    elif len(values) == 3:
        try:
            return float(values[2])
        except ValueError:
            pass
    return None


def parse_annual_guidance(html: str) -> tuple:
    """fin_year_result_d テーブルの「予」行 → (guidance_net, prior_net)"""
    m = re.search(r'fin_year_result_d.*?</table>', html, re.DOTALL)
    if not m:
        return None, None
    section = m.group(0)

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', section, re.DOTALL)
    guidance_net = None
    prior_net = None

    for i, row in enumerate(rows):
        if '予</span>' in row or '>予<' in row or '（予）' in row or '予想' in row:
            tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            nums = []
            for td in tds:
                text = re.sub(r'<[^>]+>', '', td).strip().replace(',', '')
                try:
                    nums.append(float(text))
                except ValueError:
                    nums.append(None)
            if len(nums) >= 4 and nums[3] is not None:
                guidance_net = nums[3]
            elif len(nums) >= 3 and nums[2] is not None:
                guidance_net = nums[2]
            # 直前の行（実績）を取得
            if i > 0:
                prev_tds = re.findall(r'<td[^>]*>(.*?)</td>', rows[i - 1], re.DOTALL)
                prev_nums = []
                for td in prev_tds:
                    text = re.sub(r'<[^>]+>', '', td).strip().replace(',', '')
                    try:
                        prev_nums.append(float(text))
                    except ValueError:
                        prev_nums.append(None)
                if len(prev_nums) >= 4 and prev_nums[3] is not None:
                    prior_net = prev_nums[3]
                elif len(prev_nums) >= 3 and prev_nums[2] is not None:
                    prior_net = prev_nums[2]
            break

    return guidance_net, prior_net


def fetch_kessan_data(code4: str) -> dict:
    """kabutan.jp から決算関連データを取得"""
    result: dict = {}
    try:
        html_fin = get(f'https://kabutan.jp/stock/finance/?code={code4}')
        yoy = parse_quarterly_yoy(html_fin)
        if yoy is not None:
            result['yoy_net_income_pct'] = yoy
        op_yoy = compute_operating_surprise(html_fin)
        if op_yoy is not None:
            result['yoy_operating_income_pct'] = op_yoy
        guid, prior = parse_annual_guidance(html_fin)
        if guid is not None:
            result['guidance_net_income'] = guid
        if prior is not None:
            result['prior_net_income'] = prior
    except Exception:
        pass

    time.sleep(0.3)

    try:
        html_stock = get(f'https://kabutan.jp/stock/?code={code4}')
        chg_m = re.search(r'前日比.*?([+-]?\d+\.\d+)%', html_stock, re.DOTALL)
        if chg_m:
            result['current_change_pct'] = float(chg_m.group(1))
    except Exception:
        pass

    time.sleep(0.3)
    return result


def compute_guidance_surprise(guidance, prior) -> float | None:
    if guidance is None or prior is None or prior == 0:
        return None
    return (guidance - prior) / abs(prior) * 100.0


def compute_operating_surprise(html: str) -> float | None:
    """kabutan finance ページから営業利益の前年同期比を取得する。"""
    m = re.search(r'fin_quarter_result_d.*?</table>', html, re.DOTALL)
    if not m:
        return None
    section = m.group(0)
    yoy_m = re.search(r'前年同期比</th>(.*?)</tr>', section, re.DOTALL)
    if not yoy_m:
        return None
    row_html = yoy_m.group(1)
    # 列順: 売上高[0], 営業益[1], 経常益[2], 最終益[3]
    values = re.findall(r'<span[^>]*>([-+\d.]+)</span>', row_html)
    if len(values) > 1:
        try:
            return float(values[1])
        except ValueError:
            pass
    return None


def compute_composite_surprise(yoy_pct, guidance_surprise, operating_surprise=None) -> float | None:
    if yoy_pct is not None and guidance_surprise is not None and operating_surprise is not None:
        return yoy_pct * 0.4 + guidance_surprise * 0.35 + operating_surprise * 0.25
    if yoy_pct is not None and guidance_surprise is not None:
        return yoy_pct * 0.6 + guidance_surprise * 0.4
    if yoy_pct is not None and operating_surprise is not None:
        return yoy_pct * 0.6 + operating_surprise * 0.4
    if yoy_pct is not None:
        return yoy_pct
    if guidance_surprise is not None:
        return guidance_surprise
    return None


def compute_pead_score(composite_surprise: float, text_score: int) -> float:
    sentiment_mult = max(0.5, min(1.5, 1.0 + text_score / 100.0))
    return composite_surprise * sentiment_mult


def should_filter_out(current_change_pct) -> bool:
    """T+1で既に15%超上昇済みなら除外"""
    if current_change_pct is None:
        return False
    return current_change_pct > 15.0


def scan_pead(date_list: list[str]) -> list[dict]:
    """
    Signal E のメインスキャン。pead_score 降順でソート。
    """
    print(f'Signal E (PEAD): TDNet 決算短信取得中（{len(date_list)}日分）...', file=sys.stderr)
    kessan_items = fetch_tdnet_multi_day(date_list)
    print(f'  決算短信: {len(kessan_items)} 件', file=sys.stderr)

    results = []
    for item in kessan_items:
        code = item['code']
        title = item['title']
        text_score = compute_text_score(title)

        try:
            kdata = fetch_kessan_data(code)
        except Exception:
            kdata = {}

        yoy_pct = kdata.get('yoy_net_income_pct')
        operating_pct = kdata.get('yoy_operating_income_pct')
        guidance = kdata.get('guidance_net_income')
        prior = kdata.get('prior_net_income')
        current_change_pct = kdata.get('current_change_pct')

        guidance_surprise = compute_guidance_surprise(guidance, prior)
        composite = compute_composite_surprise(yoy_pct, guidance_surprise, operating_pct)

        if should_filter_out(current_change_pct):
            continue

        # データ不足かつ text_score >= 15 の場合はテキストのみで通過（composite=0）
        if composite is None:
            if text_score >= 15:
                composite = 0.0
            else:
                continue

        text_only = (composite == 0.0 and text_score >= 15)
        if composite < 5.0 and not text_only:
            continue

        pead_score = compute_pead_score(composite, text_score)
        if pead_score < 5.0 and not text_only:
            continue

        parts = []
        if yoy_pct is not None:
            parts.append(f'前年同期比 純利益{yoy_pct:+.1f}%')
        if guidance_surprise is not None:
            parts.append(f'会社予想比{guidance_surprise:+.1f}%')
        if text_score > 0:
            parts.append(f'テキストスコア+{text_score}')
        elif text_score < 0:
            parts.append(f'テキストスコア{text_score}')
        reason = ' / '.join(parts) if parts else '決算短信'

        results.append({
            'signal': 'E',
            'code': code,
            'company': item['company'],
            'title': title,
            'date': item['date'],
            'score': min(100, max(0, int(pead_score))),
            'reason': reason,
            'pead_score': round(pead_score, 2),
            'composite_surprise': round(composite, 2) if composite is not None else None,
            'text_score': text_score,
            'current_change_pct': current_change_pct,
            'time': item.get('time', ''),
            'tdnet_url': item['tdnet_url'],
        })

    results.sort(key=lambda x: x['pead_score'], reverse=True)
    print(f'  Signal E 候補: {len(results)} 件', file=sys.stderr)
    return results


if __name__ == '__main__':
    dates = prev_biz_days(5)
    print(f'スキャン期間: {dates[-1]} 〜 {dates[0]}', file=sys.stderr)
    signals = scan_pead(dates)
    print(f'\nSignal E 結果: {len(signals)} 件')
    for s in signals[:10]:
        print(f"  ● {s['company']}({s['code']}) — {s['reason']} | pead_score={s['pead_score']}")
