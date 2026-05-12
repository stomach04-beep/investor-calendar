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
