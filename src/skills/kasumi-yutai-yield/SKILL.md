---
name: kasumi-yutai-yield
description: かすみちゃんの株主優待日記（kasumichan.com）とザイ・オンライン株主優待カテゴリ（diamond.jp/zai/category/kabunushiyutai）を日次チェックし、総合優待利回りまたは配当＋優待利回りが5%以上の日本株を抽出して専用台帳に記録する。「かすみちゃん」「kasumichan」「Zai優待」「優待利回り」「総合優待利回り5%」「優待台帳」と言われたときに使用。
---

# kasumi-yutai-yield

`https://kasumichan.com/` と `https://diamond.jp/zai/category/kabunushiyutai` を確認し、総合優待利回り・配当＋優待利回り・記事中の利回りがしきい値以上の銘柄を専用台帳に記録する。

## 日次実行

```bash
python3 src/skills/kasumi-yutai-yield/run_daily.py
```

既定値:
- 対象サイト: `https://kasumichan.com/`, `https://diamond.jp/zai/category/kabunushiyutai`
- しきい値: `5.0%`
- 取得範囲: かすみちゃんはトップページから最大2ページ分の記事リンク、Zaiはカテゴリページ内の株主優待表
- 台帳: `src/skills/kasumi-yutai-yield/results/kasumi_yutai_ledger.json`

## 確認だけ

```bash
python3 src/skills/kasumi-yutai-yield/run_daily.py --dry-run
```

## よく使うオプション

```bash
# 取得ページ数を増やす
python3 src/skills/kasumi-yutai-yield/run_daily.py --pages 5

# しきい値を変更する
python3 src/skills/kasumi-yutai-yield/run_daily.py --min-yield 6

# JSONで結果だけ出す
python3 src/skills/kasumi-yutai-yield/run_daily.py --json
```

## 記録ルール

- `total_yield_pct >= min_yield` の候補だけ記録する。
- 同一 `code + url` は重複記録しない。
- 銘柄コードが記事本文から取れない場合でも、記事URL単位の監視候補として記録する。
- Zaiはカテゴリページ内の `配当＋優待利回り` 列を優先して抽出する。
- 各レコードに `benefit_content` として優待内容を保存する。既存レコードに優待内容がない場合は、同一 `code + url` の再取得時に補完する。
- 各レコードに `holding_status` を保存する。保有銘柄は `保有中`、未保有または未判定は空文字にする。
- 実行後は新規記録の有無に関係なく、台帳全体をMarkdown表で表示する。
- 歪みスキャンの `tracker.json` とは混ぜない。優待利回り候補はこのスキル専用台帳で管理する。
- 歪みスキャンの日次フローから連携された銘柄は、コード照合で総合利回りを再取得し、5%以上だけ `source: market-distortion-signal` として追加する。
- 連携銘柄の総合利回り取得不能は「優待なし」と断定せず、優待台帳には追加しない。
- 外部サイト取得で DNS / sandbox エラーが出た場合は、同じコマンドを権限付きで再実行する。

## 出力項目

台帳には以下を保存する:

- `date`: 記録日
- `code`: 4桁銘柄コード。不明なら空文字
- `company`: 抽出できた銘柄名。不明なら空文字
- `total_yield_pct`: 抽出した最大利回り
- `source_title`: 記事タイトル
- `source_url`: 記事URL
- `matched_text`: 利回り抽出の根拠になった短い周辺テキスト
- `benefit_content`: 優待内容の要約
- `status`: `watch`
- `source`: `kasumichan.com` または `diamond.jp/zai`
- `holding_status`: 保有銘柄なら `保有中`、それ以外は空文字
