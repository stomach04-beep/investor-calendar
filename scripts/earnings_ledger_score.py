# -*- coding: utf-8 -*-
"""
Notion の「日付履歴」から、決算日の出どころ別の的中率を数える（採点スクリプト）。

背景:
  fetch_earnings.py は毎朝、決算日の出どころ（JPX / Nasdaq / Nasdaq見込み / yfinance / JQ予測 /
  EDGAR予測）を変えながら日付を更新する。どのソースがどれだけ当たるかは
  「何日前にどのソースが何と言っていたか」を残しておかないと後から数えられない。
  そこで notion_upsert.py が投資家カレンダーDBの「日付履歴」に
    2026-09-23:2026-10-29|Nasdaq見込み / 2026-10-01:2026-10-29|Nasdaq / ...
  の形で残している。本スクリプトはそれを読み、決算通過後の「実際の発表日」と突き合わせる。

実際の発表日（正解）:
  米国株 … SEC EDGAR の 8-K(Item 2.02) の reportDate（us_earnings_time.py と同じ取得）
  日本株 … data/jq_earnings_jp.json の recent_disc（J-Quants 開示履歴。手動再生成なので
           古いと直近の決算が入っていない＝その銘柄は「正解なし」で飛ばす）

出力:
  出どころ × リードタイム（発表の何日前の記録か: 1-7日 / 8-30日 / 31日以上）ごとに
  件数・ピタリ・±1日・±3日 を表にする。銘柄名は出さない（集計だけ）。

実行（NOTION_TOKEN 必須・手元で）:
  set NOTION_TOKEN=secret_xxx
  python scripts/earnings_ledger_score.py
  python scripts/earnings_ledger_score.py --json tmp/ledger_dump.json   # 取得結果を保存して再集計に使う
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import NotionClient, get_notion_db_id, log, read_date_start, read_rich_text  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LEAD_BUCKETS = ((1, 7, "1-7日前"), (8, 30, "8-30日前"), (31, 10_000, "31日以上前"))
EARNINGS_PREFIXES = ("hold_earnings_", "watch_earnings_")


def parse_log(text: str) -> list[tuple[date, date, str]]:
    """「記録日:採用日|出どころ / ...」→ [(記録日, 採用日, 出どころ), ...]。壊れた要素は飛ばす。"""
    out: list[tuple[date, date, str]] = []
    for item in (text or "").split(" / "):
        item = item.strip()
        if not item or ":" not in item or "|" not in item:
            continue
        rec, rest = item.split(":", 1)
        adopted, src = rest.split("|", 1)
        try:
            out.append((date.fromisoformat(rec.strip()), date.fromisoformat(adopted.strip()), src.strip()))
        except ValueError:
            continue
    return out


def score(entries: list[tuple[date, date, str]], actual: date) -> list[tuple[str, str, int]]:
    """1銘柄の履歴を正解日と突き合わせ [(出どころ, リード区分, ズレ日数), ...] を返す。

    各記録は「次に記録が変わる日まで有効」なので、正解日より前の記録だけを採点する。
    同じ出どころが連続していても、リード区分ごとに1件として数える（毎日同じ記録を
    水増ししない）。"""
    rows: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    for rec, adopted, src in entries:
        lead = (actual - rec).days
        if lead < 1:
            continue  # 発表当日以降の記録は「次の四半期」を指しているので採点しない
        bucket = next((lab for lo, hi, lab in LEAD_BUCKETS if lo <= lead <= hi), None)
        if bucket is None or (src, bucket) in seen:
            continue
        seen.add((src, bucket))
        rows.append((src, bucket, (adopted - actual).days))
    return rows


def summarize(rows: list[tuple[str, str, int]]) -> dict[tuple[str, str], dict]:
    agg: dict[tuple[str, str], dict] = defaultdict(lambda: {"n": 0, "exact": 0, "pm1": 0, "pm3": 0})
    for src, bucket, diff in rows:
        a = agg[(src, bucket)]
        a["n"] += 1
        a["exact"] += diff == 0
        a["pm1"] += abs(diff) <= 1
        a["pm3"] += abs(diff) <= 3
    return agg


def print_table(agg: dict[tuple[str, str], dict]) -> None:
    log("  出どころ           リード        n   ピタリ   ±1日   ±3日")
    for (src, bucket), a in sorted(agg.items()):
        n = a["n"] or 1
        log(f"  {src:14s} {bucket:10s} {a['n']:4d}  {a['exact'] * 100 // n:5d}%  {a['pm1'] * 100 // n:5d}%  {a['pm3'] * 100 // n:5d}%")


def _actual_us(tickers: list[str]) -> dict[str, list[date]]:
    """米国株の実発表日（EDGAR 8-K 2.02 の reportDate）。"""
    from us_earnings_time import us_earnings_time_map
    out: dict[str, list[date]] = {}
    for sym, row in us_earnings_time_map(tickers).items():
        out[sym] = [date.fromisoformat(d) for d in row.get("dates") or []]
    return out


def _actual_jp() -> dict[str, list[date]]:
    """日本株の実発表日（jq_earnings_jp.json の recent_disc）。"""
    try:
        payload = json.loads((ROOT / "data" / "jq_earnings_jp.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {c: [date.fromisoformat(d) for d in v] for c, v in (payload.get("recent_disc") or {}).items()}


def collect(client: NotionClient, db_id: str) -> list[dict]:
    """Notion から決算イベントの (id, 日付履歴, 現在の発表日) を集める。"""
    schema = client._request("GET", f"https://api.notion.com/v1/databases/{db_id}", None)
    props = schema.get("properties", {})
    id_name = next((n for n in props if n == "ID" or n.endswith(":ID")), "ID")
    rows: list[dict] = []
    for pg in client.query_database(db_id):
        pr = pg.get("properties", {})
        idv = read_rich_text(pr.get(id_name, {}))
        if not idv.startswith(EARNINGS_PREFIXES):
            continue
        rows.append({"id": idv,
                     "log": read_rich_text(pr.get("日付履歴", {})),
                     "current": read_date_start(pr.get("発表日時", {}))})
    return rows


def evaluate(rows: list[dict], actual_us: dict[str, list[date]], actual_jp: dict[str, list[date]],
             today: date) -> list[tuple[str, str, int]]:
    """履歴と正解を突き合わせて採点行を返す。正解は「記録より後で、今日以前の実発表日」。"""
    scored: list[tuple[str, str, int]] = []
    for r in rows:
        entries = parse_log(r["log"])
        if not entries:
            continue
        key = r["id"].rsplit("_", 1)[-1]
        actuals = actual_jp.get(key) if "_jp_" in r["id"] else actual_us.get(key)
        if not actuals:
            continue
        # 履歴の最初の記録日より後に来た実発表日を1つずつ正解にする
        first = min(rec for rec, _, _ in entries)
        for act in sorted(a for a in actuals if first < a <= today):
            # その正解に向けての記録＝正解日より前で、直前の正解日より後の記録
            prev = max((a for a in actuals if a < act), default=date.min)
            window = [e for e in entries if prev < e[0] < act]
            scored.extend(score(window, act))
    return scored


def main() -> int:
    ap = argparse.ArgumentParser(description="決算日の出どころ別の的中率を日付履歴から採点")
    ap.add_argument("--json", help="Notion から取った履歴の保存先（次回はここから読む）")
    ap.add_argument("--from-json", help="保存済み履歴から採点する（Notion に繋がない）")
    args = ap.parse_args()

    if args.from_json:
        rows = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        client = NotionClient()
        rows = collect(client, get_notion_db_id())
        if args.json:
            Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  決算イベント {len(rows)} 件（履歴あり {sum(1 for r in rows if r['log'])} 件）")
    us_syms = sorted({r["id"].rsplit("_", 1)[-1] for r in rows if "_us_" in r["id"]})
    actual_us = _actual_us(us_syms) if us_syms else {}
    actual_jp = _actual_jp()
    scored = evaluate(rows, actual_us, actual_jp, date.today())
    if not scored:
        log("  採点できる記録がまだ無い（履歴は決算通過後に採点できる）")
        return 0
    print_table(summarize(scored))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
