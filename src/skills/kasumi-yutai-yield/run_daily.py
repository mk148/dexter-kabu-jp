#!/usr/bin/env python3
"""
優待利回り情報サイトをチェックし、5%以上の候補を台帳に記録する。

Usage:
    python3 src/skills/kasumi-yutai-yield/run_daily.py
    python3 src/skills/kasumi-yutai-yield/run_daily.py --dry-run
    python3 src/skills/kasumi-yutai-yield/run_daily.py --pages 5 --min-yield 6
"""

from __future__ import annotations

import argparse
import html
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Iterable


SKILL_DIR = Path(__file__).parent
RESULTS_DIR = SKILL_DIR / "results"
LEDGER_FILE = RESULTS_DIR / "kasumi_yutai_ledger.json"
KASUMI_BASE_URL = "https://kasumichan.com/"
ZAI_CATEGORY_URL = "https://diamond.jp/zai/category/kabunushiyutai"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


@dataclass
class Candidate:
    date: str
    code: str
    company: str
    total_yield_pct: float
    source_title: str
    source_url: str
    matched_text: str
    benefit_content: str
    status: str = "watch"
    source: str = ""
    holding_status: str = ""


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="ignore")


def textify(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>", "\n", markup)
    markup = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|article)>", "\n", markup)
    text = re.sub(r"<[^>]+>", " ", markup)
    text = html.unescape(text)
    text = text.replace("\u3000", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", text)


def extract_title(markup: str) -> str:
    for pattern in [
        r"(?is)<h1[^>]*>(.*?)</h1>",
        r"(?is)<title[^>]*>(.*?)</title>",
    ]:
        match = re.search(pattern, markup)
        if match:
            return textify(match.group(1)).strip()
    return ""


def extract_article_markup(markup: str) -> str:
    match = re.search(r"(?is)<article\b[^>]*>(.*?)</article>", markup)
    if match:
        return match.group(1)
    return markup


def page_url(page_num: int) -> str:
    if page_num <= 1:
        return KASUMI_BASE_URL
    return urllib.parse.urljoin(KASUMI_BASE_URL, f"page/{page_num}/")


def collect_kasumi_post_urls(pages: int) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    for page_num in range(1, pages + 1):
        try:
            markup = fetch(page_url(page_num))
        except Exception as exc:
            print(f"[WARN] page {page_num} 取得失敗: {exc}", file=sys.stderr)
            continue

        for href in re.findall(r"""(?i)<a\s+[^>]*href=["']([^"']+)["']""", markup):
            url = urllib.parse.urljoin(KASUMI_BASE_URL, html.unescape(href))
            parsed = urllib.parse.urlparse(url)
            if parsed.netloc != "kasumichan.com":
                continue
            if not re.search(r"/\d{4}/\d{2}/\d{2}/", parsed.path):
                continue
            clean_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
            if clean_url not in seen:
                seen.add(clean_url)
                urls.append(clean_url)
        time.sleep(0.5)

    return urls


def yield_mentions(text: str) -> Iterable[tuple[float, str]]:
    compact = re.sub(r"\s+", " ", text)
    compact = re.split(r"(?:あわせて読みたい|合わせて読みたい|関連記事|新着記事|おすすめ記事)", compact)[0]
    pattern = re.compile(
        r"([^。\n]{0,45}(?:総合)?(?:優待)?利回り[^。\n]{0,45}?([0-9]+(?:\.[0-9]+)?)\s*[％%][^。\n]{0,25})"
    )
    for match in pattern.finditer(compact):
        yield float(match.group(2)), match.group(1).strip()


def title_yield_mentions(title: str) -> Iterable[tuple[float, str]]:
    for match in re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*[％%]", title):
        if "利回り" in title:
            yield float(match.group(1)), title


def company_from_title(title: str) -> str:
    cleaned = re.sub(r"[|｜].*$", "", title).strip()
    cleaned = re.sub(r"^【[^】]+】", "", cleaned).strip()
    match = re.match(r"([一-龥ぁ-んァ-ヶA-Za-z0-9＆&・ー\.\-\s]+?)(?:が|は|を|、|大幅|株主優待|優待|銘柄|到着|新設|拡充|廃止)", cleaned)
    if match:
        return match.group(1).strip()[:30]
    return ""


def extract_code(text: str, title: str) -> str:
    source = f"{title}\n{text}"
    patterns = [
        r"(?:証券コード|銘柄コード|コード)[:：\s]*([1-9]\d{3})",
        r"[（(]([1-9]\d{3})[）)]",
    ]
    for pattern in patterns:
        match = re.search(pattern, source)
        if match:
            return match.group(1)
    return ""


def extract_company(text: str, title: str, code: str) -> str:
    if code:
        around = re.search(rf"([一-龥ぁ-んァ-ヶA-Za-z0-9＆&・ー\.\-\s]{{1,30}})[（(]?{code}[）)]?", f"{title}\n{text}")
        if around:
            company = around.group(1).strip(" \n\t:：-ー")
            company = re.sub(r".*(?:目次|逆日歩|配当利回り|総合利回り|利回り|銘柄は|会社の)", "", company)
            company = re.sub(r"^(株主優待|優待|新設|変更|拡充|改悪|到着|廃止|の|を)\s*", "", company)
            company = re.sub(r"^.*(?:大手の|メーカーの|銘柄の|感じている)\s*", "", company)
            return company[:30].strip()

    title_company = company_from_title(title)
    if title_company:
        return title_company

    title_text = re.sub(r"[|｜].*$", "", title).strip()
    title_text = re.sub(r"(株主優待|優待|利回り|総合|拡充|新設|変更|廃止|到着).*$", "", title_text).strip()
    return title_text[:30]


def summarize_text(text: str, max_len: int = 120) -> str:
    text = re.sub(r"\s+", " ", text).strip(" 。\n\t")
    return text[:max_len]


def extract_kasumi_benefit(text: str, title: str) -> str:
    normalized = re.sub(r"\s+", " ", text)
    markers = ["株主優待内容", "優待内容", "優待詳細", "優待の実施概要", "廃止する優待内容"]
    stop_words = [
        "株価データ",
        "業績",
        "利回りデータ",
        "新着記事",
        "関連記事",
        "あわせて読みたい",
        "合わせて読みたい",
        "人気記事",
        "かすみちゃん おすすめ記事",
    ]

    for marker in markers:
        for match in re.finditer(marker, normalized):
            segment = normalized[match.start():match.start() + 700]
            for stop_word in stop_words:
                stop_pos = segment.find(stop_word, len(marker) + 8)
                if stop_pos != -1:
                    segment = segment[:stop_pos]
            segment = re.sub(r"^.*?" + re.escape(marker), "", segment).strip(" ：:】]。")
            segment = re.sub(r"^(?:【[^】]+】|目次|対象株数|保有株数|株主優待券の贈呈内容)\s*", "", segment)
            if re.search(r"(株以上|円分|QUOカード|デジタルギフト|優待券|食事券|クーポン|ポイント|自社商品|ギフト)", segment):
                return summarize_text(segment, 140)

    searchable = re.split(r"(?:人気記事|かすみちゃん おすすめ記事)", normalized)[0]
    patterns = [
        r"((?:[\d,]+|[一二三四五六七八九十百千]+)株以上[^。]{10,260})",
        r"((?:QUOカード|デジタルギフト|優待券|食事券|クーポン|ポイント|自社商品|ギフト|金券)[^。]{10,260})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, searchable, re.S):
            benefit = summarize_text(match.group(1), 140)
            if benefit and "関連記事" not in benefit and "新着記事" not in benefit:
                return benefit

    title_hint = re.sub(r"[|｜].*$", "", title).strip()
    return summarize_text(title_hint)


def parse_kasumi_candidate(url: str, min_yield: float, scan_date: str) -> Candidate | None:
    try:
        markup = fetch(url)
    except Exception as exc:
        print(f"[WARN] article 取得失敗: {url} {exc}", file=sys.stderr)
        return None

    title = extract_title(markup)
    article_markup = extract_article_markup(markup)
    text = textify(article_markup)
    title_mentions = list(title_yield_mentions(title))
    if title_mentions:
        mentions = [(pct, ctx) for pct, ctx in title_mentions if pct >= min_yield]
    else:
        mentions = [(pct, ctx) for pct, ctx in yield_mentions(text) if pct >= min_yield]
    if not mentions:
        return None

    pct, context = max(mentions, key=lambda item: item[0])
    code = extract_code(text, title)
    company = extract_company(text, title, code)
    benefit_content = extract_kasumi_benefit(text, title)
    return Candidate(
        date=scan_date,
        code=code,
        company=company,
        total_yield_pct=round(pct, 2),
        source_title=title,
        source_url=url,
        matched_text=context[:160],
        benefit_content=benefit_content,
        source="kasumichan.com",
    )


def clean_number(text: str) -> float | None:
    text = html.unescape(text)
    text = text.replace(",", "").replace("％", "%")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", text)
    if match:
        return float(match.group(1))
    return None


def extract_cells(row_markup: str) -> list[str]:
    return re.findall(r"(?is)<td\b[^>]*>(.*?)</td>", row_markup)


def extract_zai_benefit(block: str) -> str:
    match = re.search(r"(?is)【株主優待内容】.*?(?:</strong>)?\s*<br\s*/?>\s*(.*?)</td>", block)
    if match:
        return summarize_text(textify(match.group(1)), 140)
    text = textify(block)
    match = re.search(r"【株主優待内容】\s*(.{10,220})", text)
    if match:
        return summarize_text(match.group(1), 140)
    return ""


def collect_zai_candidates(min_yield: float, scan_date: str) -> list[Candidate]:
    try:
        markup = fetch(ZAI_CATEGORY_URL)
    except Exception as exc:
        print(f"[WARN] Zaiカテゴリ取得失敗: {exc}", file=sys.stderr)
        return []

    candidates: list[Candidate] = []
    company_pattern = re.compile(r"(?is)◆\s*([^<（(]+?)\s*[（(]([1-9]\d{3})[）)]")
    matches = list(company_pattern.finditer(markup))

    for idx, match in enumerate(matches):
        company = textify(match.group(1)).strip()
        code = match.group(2)
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else min(len(markup), start + 4000)
        block = markup[start:end]

        row_match = re.search(r"(?is)<tr\b[^>]*>(.*?)</tr>", block)
        if not row_match:
            continue

        cells = extract_cells(row_match.group(1))
        if len(cells) < 4:
            continue

        total_yield = clean_number(cells[3])
        if total_yield is None or total_yield < min_yield:
            continue

        closing_month = textify(cells[1]).strip()
        dividend_yield = textify(cells[2]).strip()
        matched = f"{company}（{code}） 配当＋優待利回り {total_yield:.2f}%"
        if dividend_yield:
            matched += f" / 配当利回り {dividend_yield}"
        if closing_month:
            matched += f" / 確定月 {closing_month}"
        benefit_content = extract_zai_benefit(block)

        candidates.append(Candidate(
            date=scan_date,
            code=code,
            company=company,
            total_yield_pct=round(total_yield, 2),
            source_title="ザイ・オンライン 株主優待おすすめ情報[2026年]",
            source_url=ZAI_CATEGORY_URL,
            matched_text=matched[:160],
            benefit_content=benefit_content,
            source="diamond.jp/zai",
        ))

    return candidates


def collect_kasumi_candidates(pages: int, min_yield: float, scan_date: str) -> tuple[list[Candidate], int]:
    post_urls = collect_kasumi_post_urls(pages)
    candidates: list[Candidate] = []
    for url in post_urls:
        candidate = parse_kasumi_candidate(url, min_yield, scan_date)
        if candidate:
            candidates.append(candidate)
        time.sleep(0.7)
    return candidates, len(post_urls)


def load_ledger() -> list[dict]:
    if LEDGER_FILE.exists():
        return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    return []


def save_ledger(records: list[dict]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def dedupe_key(record: dict) -> tuple[str, str]:
    return (str(record.get("code", "")), str(record.get("source_url", "")))


def merge_candidates_into_ledger(ledger: list[dict], candidates: list[Candidate]) -> tuple[list[dict], int, int]:
    by_key = {dedupe_key(record): record for record in ledger}
    new_count = 0
    updated_count = 0

    for candidate in candidates:
        record = asdict(candidate)
        key = dedupe_key(record)
        existing = by_key.get(key)
        if existing is None:
            ledger.append(record)
            by_key[key] = record
            new_count += 1
            continue

        changed = False
        for field in ["matched_text", "source", "company", "total_yield_pct", "source_title"]:
            if record.get(field) and not existing.get(field):
                existing[field] = record[field]
                changed = True
        if record.get("benefit_content") and record.get("benefit_content") != existing.get("benefit_content"):
            existing["benefit_content"] = record["benefit_content"]
            changed = True
        if changed:
            updated_count += 1

    return ledger, new_count, updated_count


def markdown_table(records: list[dict]) -> str:
    lines = [
        "| 保有 | # | 日付 | source | code | 銘柄 | 利回り | 優待内容 |",
        "|---|---:|---|---|---:|---|---:|---|",
    ]
    for idx, record in enumerate(records, 1):
        holding = str(record.get("holding_status") or "")
        source = str(record.get("source") or "").replace("kasumichan.com", "kasumichan").replace("diamond.jp/zai", "diamond/zai")
        code = record.get("code") or "----"
        company = summarize_text(str(record.get("company") or "銘柄不明"), 36).replace("|", " ")
        benefit = summarize_text(str(record.get("benefit_content") or "未取得"), 70).replace("|", " ")
        yld = float(record.get("total_yield_pct") or 0)
        lines.append(
            f"| {holding} | {idx} | {record.get('date', '')} | {source} | {code} | {company} | {yld:.2f}% | {benefit} |"
        )
    return "\n".join(lines)


def run(pages: int, min_yield: float, dry_run: bool, as_json: bool) -> int:
    scan_date = date.today().isoformat()
    kasumi_candidates, kasumi_checked = collect_kasumi_candidates(pages, min_yield, scan_date)
    zai_candidates = collect_zai_candidates(min_yield, scan_date)
    candidates = kasumi_candidates + zai_candidates

    ledger = load_ledger()
    ledger, new_count, updated_count = merge_candidates_into_ledger(ledger, candidates)
    new_records = [asdict(c) for c in candidates if False]

    if not dry_run and (new_count or updated_count):
        save_ledger(ledger)

    if as_json:
        print(json.dumps({
            "date": scan_date,
            "min_yield": min_yield,
            "checked_articles": kasumi_checked,
            "checked_zai_rows": len(zai_candidates),
            "candidates": [asdict(c) for c in candidates],
            "new_records_count": new_count,
            "updated_records_count": updated_count,
            "ledger": ledger,
            "dry_run": dry_run,
            "ledger_file": str(LEDGER_FILE),
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"優待利回りチェック: {scan_date}")
    print(
        f"kasumichan記事確認: {kasumi_checked}件 / "
        f"Zai候補行: {len(zai_candidates)}件 / "
        f"候補: {len(candidates)}件 / 新規記録: {new_count}件 / 内容補完: {updated_count}件"
    )

    if dry_run:
        print("\ndry-run: 台帳は更新していません")
    else:
        print(f"\n台帳: {LEDGER_FILE}")
    print("\n## 優待台帳")
    print(markdown_table(ledger))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="優待利回り5%以上候補を複数サイトから台帳記録する")
    parser.add_argument("--pages", type=int, default=2, help="取得する一覧ページ数")
    parser.add_argument("--min-yield", type=float, default=5.0, help="記録する最低利回り")
    parser.add_argument("--dry-run", action="store_true", help="台帳に書かず候補だけ表示する")
    parser.add_argument("--json", action="store_true", help="JSONで出力する")
    args = parser.parse_args()

    if args.pages < 1:
        parser.error("--pages must be >= 1")
    if args.min_yield <= 0:
        parser.error("--min-yield must be > 0")

    return run(args.pages, args.min_yield, args.dry_run, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
