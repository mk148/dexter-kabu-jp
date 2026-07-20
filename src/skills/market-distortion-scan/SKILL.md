---
name: market-distortion-scan
description: TDNet・kabutan・PEADを使って日本株の市場歪みシグナル A〜E/G（業績上方修正、大量保有、自社株買い、暴落過剰反応、決算サプライズドリフト、信用需給）を抽出し、AI判断で推奨候補を整理する。「歪みスキャンして」「市場の歪みを探して」「今日のシグナルを出して」と言われたときに使用。
---

# market-distortion-scan スキル

「歪みスキャンして」「シグナル出して」「市場の歪みを探して」と言われたら実行する。

---

## Codex / Dexter 実行メモ

- Dexter アプリ内では `market_distortion_scan` ツールを優先して使う。汎用 shell tool は前提にしない。
- Codex CLI やローカル端末では `python3 src/skills/market-distortion-scan/run_scan.py --json` を直接実行してよい。
- Codex 通常サンドボックスでは TDNet / kabutan.jp / YouTube などの外部DNS解決が失敗し、`nodename nor servname provided` や不自然な `0件` になることがある。その場合はデータなしと判断せず、権限付き実行で再取得する。
- Codex で `run_scan.py` を実行する際、直近取引日の特定失敗、TDNet 0件、値下がりランキング 0件、PEAD 0件が出たら、まずネットワーク制限を疑う。必要なら `sandbox_permissions: "require_escalated"` で同じコマンドを再実行する。
- `歪みチェックと台帳記録したあと、損益チェック` のような日次フルフロー依頼では、途中の既知ネットワーク失敗でユーザーへ最終報告して止めない。失敗した同じコマンドを権限付きで再実行し、scan -> dry-run -> record -> tracker確認 -> `entry_backtest.py --entry next-open --eval TODAY` まで続ける。
- 権限付き実行が必要になった場合は、各コマンドの承認を取りながら続行する。承認待ち以外で作業を中断せず、最後に「記録したID」「除外した候補」「損益集計」「未到来ID」をまとめる。
- JSON 取得後は、ツール結果をそのまま出すのではなく、Signal A〜E/G を下記基準で精査してレポート化する。
- `--date YYYYMMDD` 指定時は Signal E も同じ基準日から `--pead-days` 営業日分を見る。日付を混在させない。
- PEAD の `--pead-days 1` は基準日当日を含める。当日だけのPEAD確認で前営業日を拾っていないか、stderr の `Signal E 優先取得: YYYYMMDD` を必ず確認する。
- PEAD は取りこぼし防止のため最初に取得する。`run_scan.py` は Signal E を A-D/G より前に実行し、PEAD取得をタイムアウトで捨てない。
- PEADだけを確認する場合は `python3 src/skills/market-distortion-scan/run_scan.py --signal E --json --pead-days N` を使う。この場合はA-D/Gを取りに行かず、PEADに集中する。
- MCP は日次上限があるため、通常スキャンでは TDNet/kabutan 直接取得を優先する。深掘り時だけラジ株ナビ MCP を使う。

---

## 実行フロー

### 日次フルフロー（歪みチェック + 台帳記録 + 損益チェック）

ユーザーが「歪みチェックと台帳記録」「損益チェック」までまとめて依頼した場合は、この順序で最後まで実行する。

自動で一括実行する場合:
```bash
python3 src/skills/market-distortion-scan/daily_flow.py
```

定期ループで回す場合:
```bash
python3 src/skills/market-distortion-scan/daily_flow.py --loop --interval-minutes 1440
```

`daily_flow.py` はスキャンJSON保存、記録前dry-run、`取得結果` / `取得終了` / 子会社レベル / `pead_score < 70` の自動除外、歪み台帳追記確認、シグナル銘柄の優待照合（総合利回り5%以上のみ専用優待台帳へ追加）、`entry_backtest.py --entry next-open --eval TODAY` までを順に実行する。

1. スキャンJSONを保存する。
   ```bash
   python3 src/skills/market-distortion-scan/run_scan.py --json --pead-days 1 > /private/tmp/market_distortion_YYYYMMDD.json
   ```
2. 上記が `TDNetから直近取引日を特定できませんでした`、`nodename nor servname provided`、`socket.gaierror`、TDNet/PEAD/値下がりランキングが不自然な0件で失敗したら、同じコマンドを権限付きで再実行する。
3. 保存済みJSONから dry-run する。
   ```bash
   python3 src/skills/market-distortion-scan/record_signals.py --from-json /private/tmp/market_distortion_YYYYMMDD.json --dry-run
   ```
4. dry-run が株価取得失敗やDNS失敗で候補を `スキップ` した場合も、0件と判断せず同じ dry-run を権限付きで再実行する。
5. dry-run 候補をタイトル監査する。`取得結果`、`取得終了`、進捗報告、消却、処分、子会社レベルの自己株式取得、Signal E の `pead_score < 70` は記録しない。必要なら最小JSONに絞って記録する。
6. 採用候補だけを記録する。候補が0件でもワークフローは止めず、記録なしとして次の損益チェックへ進む。
   ```bash
   python3 src/skills/market-distortion-scan/record_signals.py --from-json /private/tmp/market_distortion_YYYYMMDD.json
   ```
7. `results/tracker.json` を直接読んで、追記IDまたは重複スキップを確認する。dirty tree や untracked ファイルが多い場合、`git diff` だけを監査根拠にしない。
8. 必ず損益チェックまで実行する。
   ```bash
   python3 src/skills/market-distortion-scan/entry_backtest.py --entry next-open --eval TODAY
   ```
9. `entry_backtest.py` が kabutan DNS で失敗したら、同じコマンドを権限付きで再実行する。今日追記したIDは翌営業日始値が未到来なら集計から除外されるので、未到来IDとして報告する。

### Phase 1: Python でデータ収集（自動）

アプリ内ツールが使える場合は、まず `market_distortion_scan` を使って JSON を取得する。

ツールが使えない環境では、以下の Python コマンドを実行する。

```bash
python3 src/skills/market-distortion-scan/run_scan.py --json 2>/dev/null
```

Codex で上記が失敗または不自然に空の場合は、stderr を捨てずに原因を確認する。

```bash
python3 src/skills/market-distortion-scan/run_scan.py --json
```

DNS/ネットワーク制限が原因なら、Codex の権限付き実行で同じコマンドを再実行する。権限付き実行では TDNet / kabutan.jp へ直接到達できることを前提にする。

- TDNet 全開示をスクレイピングし Signal A/C/D 候補を抽出
- Signal B: 大量保有報告書を検出
- Signal E (PEAD): 最初に `--pead-days` で指定した直近営業日数の決算短信から複合サプライズ銘柄を検出
- Signal F (メディアアルファ): YouTube投資系チャンネルから銘柄インパクトを抽出（`python3 src/skills/market-distortion-scan/run_scan.py --signal F --json` または `python3 src/skills/media-alpha/run_weekly.py --json` で実行）
- Signal G (信用需給): 信用倍率 >= 3.0 または前週比+50%以上の需給ひっ迫候補
- kabutan.jp から暴落銘柄・基本財務を取得
- 結果を JSON で stdout に出力

特定日を指定する場合:
```bash
python3 src/skills/market-distortion-scan/run_scan.py --json --date YYYYMMDD 2>/dev/null
```

PEAD の遡り営業日数を絞る場合:
```bash
python3 src/skills/market-distortion-scan/run_scan.py --signal E --json --pead-days 1
python3 src/skills/market-distortion-scan/run_scan.py --signal E --json --pead-days 5
python3 src/skills/market-distortion-scan/run_scan.py --json --pead-days 1
python3 src/skills/market-distortion-scan/run_scan.py --json --date YYYYMMDD --pead-days 1
```

日次の台帳記録では、前営業日の重複取得を避けるため `record_signals.py` が既定で `--pead-days 1` を `run_scan.py` に渡す。実行漏れや連休明けの補完が必要な場合だけ `record_signals.py --pead-days 2` 以上を明示する。

Codex で日付指定スキャンが `total_disclosures=0` になった場合も、取引所休業日やTDNet側の公開状態と即断しない。まず権限付き実行で同じ `--date` コマンドを再実行し、通常サンドボックスのDNS制限との差を確認する。

Signal D/E のバックテスト台帳:
```bash
python3 src/skills/market-distortion-scan/backtest.py collect
python3 src/skills/market-distortion-scan/backtest.py daily
python3 src/skills/market-distortion-scan/backtest.py update
python3 src/skills/market-distortion-scan/backtest.py stats
python3 src/skills/market-distortion-scan/backtest.py stats --recommended-only
```

推奨シグナルをトラッカー台帳へ記録:
```bash
python3 src/skills/market-distortion-scan/record_signals.py
python3 src/skills/market-distortion-scan/record_signals.py --dry-run
python3 src/skills/market-distortion-scan/record_signals.py --pead-days 3 --dry-run
```

日次の歪みチェックと台帳記録を依頼された場合は、まず dry-run で候補を見て、採用候補が妥当なときだけ記録する。通常の日次運用では PEAD は `--pead-days 1` のままにする。前回実行漏れ、連休明け、またはユーザーが明示した場合だけ `--pead-days 2` 以上に広げる。

台帳記録の守るべき境界:
- `results/tracker.json` は推奨候補だけを入れる本命台帳。PEADの母集団や監視候補を入れない。
- Signal E は `recommended=true` かつ `pead_score >= 70` のみ本命台帳へ記録する。
- 記録時は `event_date`, `title`, `reason`, `tdnet_url`, `financials`, `pead_candidate` を保存し、後から根拠監査できるようにする。
- `(date, code, signal)` の重複は作らない。既存重複を見つけた場合は先に記録されたIDを残し、削除対象は `results/quarantine_*.json` に退避してから外す。
- 過剰記録を疑う場合は、削除ではなく隔離を優先する。隔離ファイルには `quarantine_reason` を残す。

全PEAD候補の検証用台帳（推奨台帳とは別管理）:
```bash
python3 src/skills/market-distortion-scan/pead_candidates.py collect --dry-run
python3 src/skills/market-distortion-scan/pead_candidates.py collect
python3 src/skills/market-distortion-scan/pead_candidates.py collect --pead-days 5
python3 src/skills/market-distortion-scan/pead_candidates.py list --limit 30
python3 src/skills/market-distortion-scan/pead_candidates.py stats
```

`pead_candidates.py` は `results/pead_candidates.json` に Signal E 候補を保存する。`tracker.json` は推奨候補だけを入れる本命台帳、`pead_candidates.json` は PEAD 検証の母集団台帳として分けて扱う。

```bash
python3 src/skills/market-distortion-scan/record_signals.py --dry-run
python3 src/skills/market-distortion-scan/record_signals.py
python3 src/skills/market-distortion-scan/ledger_deep_dive.py --latest --budget 1000000 --write
```

台帳銘柄の高速深掘り・100万円枠比較・コメント追記:
```bash
python3 src/skills/market-distortion-scan/ledger_deep_dive.py --latest --dry-run
python3 src/skills/market-distortion-scan/ledger_deep_dive.py --latest --budget 1000000 --write
python3 src/skills/market-distortion-scan/ledger_deep_dive.py --date YYYY-MM-DD --budget 1000000 --json
```

`ledger_deep_dive.py` は株探の銘柄ページとスマホ版時系列ページを並列取得し、終値、52週高値/安値、52週高値からの下落率、PER/PBR、配当利回り、信用倍率、時価総額、100株単位での購入可能株数を計算する。`--write` 指定時は `results/tracker.json` の `buy_thesis` / `exit_condition` / `deep_dive` を更新する。Codex サンドボックスで DNS エラーが出た場合は権限付き実行で再実行する。

台帳記録の翌営業日エントリー検証:
```bash
python3 src/skills/market-distortion-scan/entry_backtest.py
python3 src/skills/market-distortion-scan/entry_backtest.py --compare
python3 src/skills/market-distortion-scan/entry_backtest.py --entry next-open --eval TODAY
python3 src/skills/market-distortion-scan/entry_backtest.py --entry rebound-low-plus --rebound-pct 2 --compare
```

`entry_backtest.py` は `results/tracker.json` を読み、kabutan スマホ版の日足から、シグナル翌営業日に取引した仮定の損益を計算する。出力は全角銘柄名でも桁が崩れにくい固定幅表にする。

- `signal-close`: シグナル記録価格でエントリー
- `next-open`: シグナル翌営業日の始値でエントリー
- `next-close`: シグナル翌営業日の終値でエントリー
- `rebound-low-plus`: シグナル翌営業日の安値から指定率だけ反発した価格でエントリー。`--rebound-pct 2` なら「翌日安値+2%反発」。日足では安値到達後に反発した順序までは厳密に確認できないため、簡易判定として扱う。

`--compare` は以下をまとめて出す。
- 全件 / 本命+上位候補 / Signal別 / 発生日別の集計
- エントリー条件比較: シグナル日終値、翌日始値、翌日終値、翌日安値+N%反発
- 評価時点比較: T1、T3、T5、TODAY

全体がマイナス傾向のときは、すぐに「シグナルが悪い」と結論しない。少なくとも、(1) 翌日始値で追いかけすぎていないか、(2) Signal C/D/E のどれが損益を悪化させているか、(3) T+3/T+5 まで未到達で短すぎないか、(4) 翌日終値や反発確認エントリーで損益が改善するか、を比較してから判断する。

毎営業日の自動収集（ローカル crontab）:
```bash
python3 src/skills/market-distortion-scan/backtest.py print-cron
python3 src/skills/market-distortion-scan/backtest.py install-cron --hour 18 --minute 10
```

---

### Phase 2: Codex / Dexter Agent が JSON を読んで判断する

JSON の `signal_a` / `signal_b` / `signal_c` / `signal_d` / `signal_e` を以下の基準で精査する。

> **小型株優先ルール**: `financials.mktcap` が 5000〜50000（百万円 = 50〜500億円）の銘柄は
> アナリストカバレッジが薄く情報優位が大きい。同スコアなら小型株を優先する。

#### Signal A（業績修正）の判断

各候補に `tdnet_url` が含まれる。**WebFetch でそのURL にアクセスし、開示内容を確認すること。**

- **採用**: 上方修正（売上・営業利益・純利益のいずれかが増額）が確認できた銘柄
- **除外**: 下方修正、配当修正のみ、将来予想の注釈のみ、訂正
- **加点**: 通期への影響が大きい、修正幅が 10%超、ROE ≥ 8% かつ FCF > 0

> TDNet PDFが直接読めない場合は `tdnet_url` のHTMLページを確認する。
> それも困難な場合は開示タイトルと financials で判断する。

**Signal A 採用銘柄には必ず以下の「買いテーゼ」を生成する:**

```
【質スコア】X/5
  - 修正内容: 売上/営業利益/純利益（増額 or 減額）
  - 修正幅: +XX%（通期予想比）
  - 修正起因: 収益増 / コスト削減 / 一時要因 / 不明
  - 継続性: 通期への寄与あり / 一時的
買いテーゼ: ～～～（2〜3文。なぜ今が買いか）
出口条件: +XX%到達 or XX日後 or [次回決算前]
リスク: ～～（この銘柄を諦める条件）
```

質スコア基準: 5=収益起因+修正幅15%超+通期継続 / 4=収益起因+修正幅10%超 / 3=収益起因 or 修正幅5〜10% / 2=コスト削減のみ or 一時要因 / 1=内容不明瞭

★推奨対象は質スコア **3以上** の銘柄のみとする。

#### Signal B（大量保有）の判断

各候補に `tdnet_url` が含まれる。**WebFetch でアクセスし保有者・比率を確認する。**

- **採用条件**: 機関投資家（ファンド・証券・アセットマネジメント等）による新規取得 or 増加
- **加点**: アクティビスト系（Oasis、エリオット、シティインデックス等）/ 時価総額500億未満
- **除外**: 個人保有者 / 保有減少 / インデックスファンドの調整

#### Signal C（自社株買い）の判断

- **採用条件**: PBR < 1.0（割安で買い戻す意義がある）
- **加点**: 取得上限額が時価総額の 1%超、FCF > 0
- **除外**: 取得状況報告・消却・処分のみ

#### Signal E（PEAD 決算サプライズドリフト）の判断

`--pead-days` で指定した直近営業日分の決算短信をスキャンし、複合サプライズスコアが高い銘柄を検出する。

- **採用条件**: `composite_surprise >= 5%` かつ `pead_score >= 5`
- **加点**: `composite_surprise >= 10%` かつ ROE >= 8%（推奨）
- **除外**: `current_change_pct > 15%`（ドリフト済み）、テキストがネガティブ

スコア構成:
- `composite_surprise = yoy_pct * 0.6 + guidance_surprise * 0.4`
- `pead_score = composite_surprise * sentiment_mult`（sentiment_mult は 0.5〜1.5）

データ不足で `composite_surprise` が取得できない場合でも、タイトルに「最高益」「増収増益」等のポジティブキーワード（text_score >= 15）があれば候補として出力する。

#### Signal D（暴落過剰反応）の判断

各候補に `same_day_disclosures`（同日の開示リスト）が含まれる。

**真の悪材料があれば除外する（ルールだけでなく AI 判断で確認する）:**
- 下方修正、業績悪化、不正・調査・行政処分
- 大規模減損、訴訟リスク顕在化、引当金計上

**Dは原則として監視寄り。推奨台帳に入れる条件は厳格にする。**

**推奨条件 v2（すべて満たす）:**
- 同日開示なし、または軽微な開示のみ
- ROE ≥ 5% かつ FCF > 0
- PER < 20
- PBR < 2.0
- 配当利回り ≥ 3%
- 時価総額 50〜500億円
- 52週高値比 -40%以内

**除外・監視扱い:**
- 高PBR成長株の急落
- 52週高値比 -40%超の下降トレンド疑い
- PER 20倍以上、PBR 2倍以上、低利回り
- 財務データ不足で上記条件を確認できない銘柄
- 真の悪材料が見つからなくても、構造的下落が疑われる銘柄

---

### Phase 3: スコアリングとレポート出力

採用した銘柄を以下フォーマットで出力する:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
本日の市場歪みシグナル（AI判断）  YYYY-MM-DD
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A: 業績上方修正（PDF確認済み）
  ● 銘柄名(コード) — 質スコアX/5 / 根拠1〜2行 / ROE XX% / FCF 黒字  ★推奨
    買いテーゼ: ～～～
    出口: +XX% or XX日後 | リスク: ～～～
  ...

B: 大量保有出現
  ● 銘柄名(コード) — 保有者名 XX%取得 / 時価総額XX億  ★推奨
  ...

C: 自社株買い
  ● 銘柄名(コード) — 根拠 / PBR X.XX / FCF 黒字  ★推奨
  ...

D: 暴落過剰反応
  ● 銘柄名(コード) — 前日比-XX% / 同日悪材料なし / 根拠  ★推奨
  ...

E: PEAD 決算サプライズドリフト
  ● 銘柄名(コード) — composite_surprise XX% / pead_score X.X / ROE XX% / ドリフト未消化  ★推奨
  ...

F: メディアアルファ（YouTube投資系）
  ● 銘柄名(コード) — チャンネル名 / テーマ / 確信度XX%  ★推奨
  ...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
★推奨: X件  |  スキャン対象: 適時開示XXX件
次のアクション: 「急騰リサーチして」→ カタリスト3層構造解剖 & 類似候補スコアリング
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

### Phase 4: 深掘り（ユーザーが要求した場合）

「この銘柄をもっと詳しく」と言われた場合:
- 台帳内の複数銘柄比較、購入予算、52週高値からの下落率を含むお買い得判定なら、まず `ledger_deep_dive.py --latest --budget 金額 --dry-run` を実行する。
- ユーザーが台帳追記を求めたら `ledger_deep_dive.py --latest --budget 金額 --write` を実行し、`buy_thesis` / `exit_condition` / `deep_dive` を保存する。
- ラジ株ナビ MCP `get_edinet_financial_data` で詳細財務取得
- または `src/skills/comprehensive-analysis/SKILL.md` の総合分析を実行

> ラジ株ナビ MCP は日次 50 回上限あり。深掘り対象は上位 3〜5 件に限定する。

---

## 検出シグナル

| シグナル | 内容 | AI 判断基準 |
|---------|------|----------------|
| A: 業績上方修正 | TDNet 開示 + PDF 確認 + 買いテーゼ生成 | 質スコア ≥ 3 / ROE ≥ 8% / FCF > 0 |
| B: 大量保有出現 | TDNet 大量保有報告書 | 機関投資家新規 / 時価総額500億未満優先 |
| C: 自社株買い | 自己株式取得決議 | PBR < 1.0 / 取得規模 1%超 |
| D: 暴落過剰反応 | 前日比 -8%以上 | 監視寄り。推奨は PER<20 / PBR<2 / 利回り≥3% / 時価総額50〜500億 / 52週高値比-40%以内 |
| E: PEAD ドリフト | `--pead-days` 分の決算短信 | composite_surprise ≥ 10% / ROE ≥ 8% / ドリフト未消化 |
| F: メディアアルファ | YouTube投資系チャンネル | 確信度 ≥ 0.6 / 複数チャンネルで言及されたテーマ優先 |

## データソース

- 適時開示: TDNet 直接スクレイピング（MCP 不使用）
- 値下がりランキング: kabutan.jp（東証プライム）
- 財務: kabutan.jp 上位 5〜8 件のみ

## 関連スキル

| 操作 | スキル |
|------|--------|
| Signal E 候補の「なぜ急騰したか」を解剖 / 次の急騰候補をAI供給チェーンから探す | `src/skills/surge-research/SKILL.md` |
| 採用銘柄の総合分析（DCF・SEPA・ダウ理論等） | `src/skills/comprehensive-analysis/SKILL.md` |

Signal E で `pead_score >= 30` または Signal A で質スコア 4〜5 の銘柄が出た場合、続けて「急騰リサーチして」と言うと surge-research スキルが起動し、カタリスト3層構造と類似候補スコアリングを行う。

## 注意事項

- このスキルは情報提供のみ。投資判断はユーザー自身が行う。
- テキストレポートのみが必要な場合: `--json` フラグなしで実行すれば従来のルールベース出力が得られる。
