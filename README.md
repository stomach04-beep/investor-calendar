# investor-calendar

投資家カレンダー（経済イベント）の自動更新パイプライン。

## 役割

```
[公式サイト / 暦計算]
  ↓ 毎日 06:35 JST (GitHub Actions)
[scripts/build_events.py]  各カテゴリのイベントを構築
  ↓
[scripts/fetch_fomc.py / fetch_boj.py]  公式サイトから日程を確定
  ↓
[scripts/notion_upsert.py]  Notion DB に upsert（is_estimated=false 行は保護）
  ↓
[Notion DB: 投資家カレンダー（7aaf88b59b714c628e1114391f4636f3）]
  ↓
[scripts/notion_to_json.py]  Notion 全件 → data/investor_events.json
  ↓ git auto-commit
[raw.githubusercontent.com/stomach04-beep/investor-calendar/main/data/investor_events.json]
  ↓ HTTP GET (30日周期 WorkManager)
[Android アプリ InvestorCalendar]
```

## 配信先

- Notion DB（人が編集できる中間ストア）: <https://www.notion.so/7aaf88b59b714c628e1114391f4636f3>
- 公開 JSON（アプリ取得用）: <https://raw.githubusercontent.com/stomach04-beep/investor-calendar/main/data/investor_events.json>

## カテゴリ別自動化

| カテゴリ | 取得方法 |
|---|---|
| FOMC | fetch_fomc.py が FRB 公式から取得、失敗時はパターン投入 |
| BOJ | fetch_boj.py が日銀公式から取得、失敗時はパターン投入 |
| JOBS / CPI / PCE / GDP / TANKAN / DIVIDEND / EARNINGS / MARKET / EVENT | build_events.py が暦と前年パターンから is_estimated=true で生成 |

## 決算日（EARNINGS）の精度

保有株・ウォッチ銘柄の決算日は `fetch_earnings.py` が毎朝取り直す。
出どころを上から順に試し、**最初に取れたものを採用**する（どれも「今日以降」でなければ捨てる）。

| 順 | 日本株 | 米国株 | 確度 |
|---|---|---|---|
| 1 | JPX 公式発表予定（約1ヶ月先まで） | Nasdaq 公式カレンダー（約5週先まで） | 取引所公式＝ほぼ確定 |
| 2 | yfinance | yfinance | Yahoo の予定日（推定を含む） |
| 3 | J-Quants 開示履歴からの予測 | SEC EDGAR 8-K(2.02) 実績からの予測 | 下記の実測精度 |

- 公式ソースは**近い決算しか載らない**。2〜3ヶ月先の決算は必ず 2〜3 段目になる＝「先の予定ほど粗い」。
- yfinance の日付は `get_earnings_dates` の直近未来行から採る。`calendar["Earnings Date"]` は
  **引け後(AMC)発表の銘柄で必ず1日後ろにズレる**（Nasdaq公式との突合で 27件中27件が+1日。
  `get_earnings_dates` なら AM/PM とも 100% 一致・2026-08-26 実測 150銘柄）。
- 土日・祝日に決算発表は無いので、推定・予測由来の日付が休場日に当たったら翌営業日へずらす
  （東証の休場日は `data/jp_market_holidays.json`＝J-Quants の取引カレンダー由来。約1年ぶん）。
  例: 任天堂は昨年 11/4(火) 発表 →+364日で 2026-11-03 になるが文化の日なので 11/04 に補正。
- どの段から来たかは各イベントの `date_source` と Notion の説明欄に残す。
- 採用した日付が実績予測と 4 日以上ズレたら説明欄に ⚠️ を付ける（どちらが正しいかは決めない）。

### 予測ルールと実測精度

ルールは日米共通で「**昨年の同じ四半期の実発表日 + 364日**」（52週＝曜日が保たれる）。
候補が複数あるときは「前回発表 + 91日×n」で見積もった次の四半期に**最も近い**候補を採る。
素朴に「今日以降で最も早い候補」を採ると、履歴に決算以外の開示が1件混ざるだけで
3ヶ月手前の日付を拾う（下表の差）。

`python scripts/earnings_accuracy.py` で測り直せる（過去の実発表日ごとに、
その時点より前の履歴だけで予測を作って突き合わせるアウトオブサンプル検証）。

| 検証 | 件数 | ピタリ | ±1日 | ±3日 | ±7日 | 平均ズレ |
|---|---|---|---|---|---|---|
| 日本株・最も早い候補 | 137,593 | 30.8% | 58.7% | 75.0% | 89.2% | 8.9日 |
| 日本株・四半期に合わせる | 137,593 | **33.4%** | **63.4%** | **80.9%** | **96.2%** | **2.5日** |
| 米国株・最も早い候補 | 454 | 44.1% | 57.0% | 64.5% | 82.8% | 12.1日 |
| 米国株・四半期に合わせる | 454 | **49.3%** | **63.4%** | **72.2%** | **91.2%** | **4.9日** |

（2026-08-27 実測。米国株は AAPL/KO/VZ など16銘柄の 8-K 実績、日本株は J-Quants の10年開示履歴）

⚠️ 「四半期に合わせる」を実際に使っているのは**米国株の EDGAR 予測だけ**。
日本株の J-Quants 予測は `data/jq_earnings_jp.json`（jquants-bulk で事前生成した候補日リスト）から
「今日以降で最も早い候補」を選ぶ＝上表の「最も早い候補」のまま。
こちらも四半期に合わせるには、生成側に「最後の実開示日」を持たせてファイルを作り直す必要がある（未実施）。

公式カレンダーを正解とした「今この瞬間の」突合（2026-08-26〜27 実測）:

| 正解 | ソース | 取得できた数 | ピタリ | ±1日 | ±3日 |
|---|---|---|---|---|---|
| Nasdaq公式 | yfinance `get_earnings_dates` | 114/150 | 71%（AM/PM別では各100%） | 72% | 74% |
| Nasdaq公式 | yfinance `calendar` (旧実装) | 116/150 | 33%（PM銘柄は0%） | 69% | 72% |
| Nasdaq公式 | EDGAR実績予測 | 98/150 | 53% | 61% | 64% |
| JPX公式 | yfinance | 11/80 | 72% | 81% | 81% |
| JPX公式 | J-Quants予測 | 80/80 | 35% | 61% | 95% |

日本株で yfinance を J-Quants 予測より上に置いているのは、当たるが**小型株の日付をほとんど返さない**ため
（80銘柄中11銘柄しか取れない）。広く埋めるのは J-Quants 予測の役目。

⚠️ **このリポジトリは公開**。保有銘柄が Actions のログから読めてしまうため、
`fetch_earnings.py` は GitHub Actions 上では銘柄名入りの行を出さず、件数の集計だけを出す
（`detail_log`）。手元で実行したときは従来どおり全部出る。

## 設計上の決定

- **`is_estimated=false` の行は notion_upsert.py が上書きしない**（人の手動補正を尊重）
- Notion API トークンは GitHub Secrets のみで管理（アプリには埋め込まない）
- 朝ブリーフ統一スケジュール 06:35 JST に編入（5分刻み分散）

## ローカル実行

```powershell
$env:NOTION_TOKEN = "secret_xxxxx"
$env:NOTION_DB_ID = "7aaf88b59b714c628e1114391f4636f3"
python scripts/build_events.py --year 2026
python scripts/fetch_fomc.py
python scripts/fetch_boj.py
python scripts/notion_upsert.py
python scripts/notion_to_json.py
```
