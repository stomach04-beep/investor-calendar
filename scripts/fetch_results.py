"""
終了したイベントの実績値を FRED（米セントルイス連銀の公開CSV・APIキー不要）から
取得し、Notion 投資家カレンダーDB の「結果」欄へ自動記入する。

対象カテゴリ（米国主要指標のみ）:
- FOMC : 政策金利ターゲットレンジ（DFEDTARL / DFEDTARU）＋据え置き/利上げ/利下げ判定
- JOBS : 非農業部門雇用者数の前月差（PAYEMS）＋失業率（UNRATE）
- CPI  : 総合・コアの前年同月比（CPIAUCSL / CPILFESL）
- PCE  : 総合・コアの前年同月比（PCEPI / PCEPILFE）
- GDP  : 実質GDP成長率 年率（A191RL1Q225SBEA）

仕様:
- 過去（発表日時 < 現在）かつ「結果」が空のイベントだけ対象（手入力・既記入は上書きしない）
- 日本イベント / ベージュブック / 決算 / 配当などは対象外（定性的・個別すぎるため）
- FRED 取得はベストエフォート（失敗 series はスキップ）。タイムアウトは指数バックオフでリトライ

実行:
    set NOTION_TOKEN=secret_xxx
    python scripts/fetch_results.py             # 本番（Notion書き込み）
    python scripts/fetch_results.py --dry-run   # Notion読み取りのみ、書き込みなし
    python scripts/fetch_results.py --self-test  # Notion不要、FRED取得＋結果生成だけ確認
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    NotionClient,
    get_notion_db_id,
    log,
    prop_rich_text,
    read_date_start,
    read_rich_text,
    read_select,
)


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) investor-calendar-bot/1.0"
# cosd（観測開始日）で直近のみ取得し、日次series（FF金利等）の転送量を抑える
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={cosd}"
FRED_COSD = "2023-01-01"  # YoY計算・FOMC比較に十分。2027年以降のイベント対応時は適宜更新

# カテゴリ → 使用する FRED series
CATEGORY_SERIES = {
    "FOMC": ["DFEDTARL", "DFEDTARU"],
    "JOBS": ["PAYEMS", "UNRATE"],
    "CPI": ["CPIAUCSL", "CPILFESL"],
    "PCE": ["PCEPI", "PCEPILFE"],
    "GDP": ["A191RL1Q225SBEA"],
}


# ----------------------------------------------------------------------
# FRED 取得
# ----------------------------------------------------------------------
def load_fred(series_id: str, retries: int = 4) -> list[tuple[date, float]]:
    """FRED 公開CSVを [(observation_date, value), ...] 昇順で返す。欠損(.)は除外。
    cosd で観測開始日を絞り、転送量を抑えて安定取得する（日次series のタイムアウト対策）。"""
    url = FRED_CSV.format(sid=series_id, cosd=FRED_COSD)
    for attempt in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
            if r.status_code != 200:
                log(f"  FRED {series_id}: HTTP {r.status_code}")
                return []
            out: list[tuple[date, float]] = []
            reader = csv.reader(io.StringIO(r.text))
            next(reader, None)  # ヘッダ行
            for row in reader:
                if len(row) < 2:
                    continue
                ds, vs = row[0].strip(), row[1].strip()
                if not ds or vs in (".", ""):
                    continue
                try:
                    out.append((date.fromisoformat(ds), float(vs)))
                except ValueError:
                    continue
            out.sort()
            return out
        except Exception as e:
            wait = 2 ** attempt
            log(f"  FRED {series_id} 取得失敗(試行{attempt+1}/{retries}: {type(e).__name__}) {wait}秒後再試行")
            time.sleep(wait)
    log(f"  FRED {series_id}: リトライ上限、スキップ")
    return []


def latest_on_or_before(series: list[tuple[date, float]], d: date):
    """d 以前で最新の (date, value)。無ければ None。"""
    pick = None
    for dt, v in series:
        if dt <= d:
            pick = (dt, v)
        else:
            break
    return pick


def prev_month(d: date) -> tuple[int, int]:
    """d の前月を (year, month) で返す。"""
    if d.month == 1:
        return d.year - 1, 12
    return d.year, d.month - 1


def month_value(series: list[tuple[date, float]], year: int, month: int):
    """series から指定年月（月初 observation）の (date, value) を返す。無ければ None。"""
    for dt, v in series:
        if dt.year == year and dt.month == month:
            return (dt, v)
    return None


# ----------------------------------------------------------------------
# カテゴリ別の結果文字列生成
# ----------------------------------------------------------------------
def build_result(category: str, event_date: date, cache: dict) -> str | None:
    """カテゴリとイベント日から、FRED 実績値で結果文字列を作る。取得不能なら None。"""
    def s(sid: str) -> list[tuple[date, float]]:
        if sid not in cache:
            cache[sid] = load_fred(sid)
        return cache[sid]

    if category == "FOMC":
        lo = latest_on_or_before(s("DFEDTARL"), event_date)
        hi = latest_on_or_before(s("DFEDTARU"), event_date)
        if not lo or not hi:
            return None
        # 1週間前の上限と比べて据え置き/利上げ/利下げを判定
        prev = latest_on_or_before(s("DFEDTARU"), event_date - timedelta(days=7))
        if prev and hi[1] > prev[1] + 1e-9:
            change = "利上げ"
        elif prev and hi[1] < prev[1] - 1e-9:
            change = "利下げ"
        else:
            change = "据え置き"
        return f"政策金利 {lo[1]:.2f}–{hi[1]:.2f}%（{change}）"

    if category == "JOBS":
        ty, tm = prev_month(event_date)  # 対象月＝発表日の前月（前月分を翌月頭に発表）
        pay = s("PAYEMS")
        cur = month_value(pay, ty, tm)
        if not cur:
            return None
        py, pm = prev_month(date(ty, tm, 1))
        prev = month_value(pay, py, pm)
        une = month_value(s("UNRATE"), ty, tm)
        parts: list[str] = []
        if prev:
            diff_man = (cur[1] - prev[1]) / 10.0  # 千人 → 万人
            parts.append(f"非農業部門雇用者数 前月比{diff_man:+.1f}万人")
        if une:
            parts.append(f"失業率 {une[1]:.1f}%")
        if not parts:
            return None
        return " / ".join(parts) + f"（{ty}年{tm}月分）"

    if category in ("CPI", "PCE"):
        ty, tm = prev_month(event_date)  # 対象月＝発表日の前月
        head_id, core_id = CATEGORY_SERIES[category]
        head = s(head_id)
        core = s(core_id)
        hcur = month_value(head, ty, tm)
        hprev = month_value(head, ty - 1, tm)  # 前年同月
        ccur = month_value(core, ty, tm)
        cprev = month_value(core, ty - 1, tm)
        parts = []
        if hcur and hprev:
            parts.append(f"{category} 前年比{(hcur[1] / hprev[1] - 1) * 100:+.1f}%")
        if ccur and cprev:
            parts.append(f"コア{(ccur[1] / cprev[1] - 1) * 100:+.1f}%")
        if not parts:
            return None
        return " / ".join(parts) + f"（{ty}年{tm}月分）"

    if category == "GDP":
        g = s("A191RL1Q225SBEA")
        cur = latest_on_or_before(g, event_date)
        if not cur:
            return None
        quarter = (cur[0].month - 1) // 3 + 1
        return f"実質GDP {cur[1]:+.1f}%（{cur[0].year}年Q{quarter}・年率）"

    return None


# ----------------------------------------------------------------------
# Notion プロパティ名の解決
# ----------------------------------------------------------------------
def resolve_props(client: NotionClient, db_id: str) -> dict[str, str]:
    url = f"https://api.notion.com/v1/databases/{db_id}"
    data = client._request("GET", url, None)
    props = data.get("properties", {})

    def find(jp: str, typ: str) -> str:
        if jp in props and props[jp].get("type") == typ:
            return jp
        for name, meta in props.items():
            if name.strip() == jp and meta.get("type") == typ:
                return name
        raise RuntimeError(f"プロパティ '{jp}' (type={typ}) が見つかりません")

    # ID は "ID" or "userDefined:ID"
    id_name = "ID"
    if id_name not in props:
        for name in props:
            if name.lower() == "id" or name.endswith(":ID") or name.endswith(":id"):
                id_name = name
                break

    return {
        "id": id_name,
        "category": find("カテゴリ", "select"),
        "country": find("国", "select"),
        "datetime": find("発表日時", "date"),
        "result": find("結果", "rich_text"),
    }


# ----------------------------------------------------------------------
# メイン
# ----------------------------------------------------------------------
def run_self_test() -> int:
    """Notion を使わず、サンプル日付で結果生成を確認する。"""
    cache: dict = {}
    samples = [
        ("FOMC", date(2026, 1, 28)), ("FOMC", date(2026, 4, 29)),
        ("JOBS", date(2026, 2, 6)), ("JOBS", date(2026, 6, 5)),
        ("CPI", date(2026, 3, 11)), ("CPI", date(2026, 5, 12)),
        ("PCE", date(2026, 2, 27)), ("PCE", date(2026, 5, 29)),
        ("GDP", date(2026, 4, 29)),
    ]
    print("=== fetch_results self-test（FRED実取得） ===")
    for cat, d in samples:
        res = build_result(cat, d, cache)
        print(f"{cat:5} {d}  ->  {res}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="終了イベントの結果を FRED から Notion 結果欄へ記入")
    parser.add_argument("--dry-run", action="store_true", help="Notion 読み取りのみ、書き込みしない")
    parser.add_argument("--self-test", action="store_true", help="Notion 不要、FRED取得＋生成のみ確認")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    db_id = get_notion_db_id()
    client = NotionClient()
    name_map = resolve_props(client, db_id)
    log(f"  プロパティ解決: 結果='{name_map['result']}', カテゴリ='{name_map['category']}'")

    pages = client.query_database(db_id)
    log(f"  Notion DB {len(pages)} ページ取得")

    now = datetime.now(timezone.utc)
    cache: dict = {}
    targets = 0
    written = 0
    skipped_future = 0
    skipped_filled = 0

    for page in pages:
        props = page.get("properties", {})
        category = read_select(props.get(name_map["category"], {}))
        if category not in CATEGORY_SERIES:
            continue
        # FRED は米国指標のみ。日本の CPI/GDP 等に米国値を入れないよう国=US に限定
        country = read_select(props.get(name_map["country"], {}))
        if country != "US":
            continue
        existing_result = read_rich_text(props.get(name_map["result"], {}))
        if existing_result and existing_result.strip():
            skipped_filled += 1
            continue
        dt_start = read_date_start(props.get(name_map["datetime"], {}))
        if not dt_start:
            continue
        try:
            ev_dt = datetime.fromisoformat(dt_start)
        except ValueError:
            continue
        # tz が無ければ UTC とみなす
        if ev_dt.tzinfo is None:
            ev_dt = ev_dt.replace(tzinfo=timezone.utc)
        if ev_dt > now:
            skipped_future += 1
            continue

        targets += 1
        res = build_result(category, ev_dt.date(), cache)
        if not res:
            log(f"  結果生成できず（FRED未取得?）: {category} {ev_dt.date()}")
            continue

        if args.dry_run:
            log(f"  [dry-run] {category} {ev_dt.date()} -> {res}")
            written += 1
        else:
            try:
                client.update_page(page["id"], {name_map["result"]: prop_rich_text(res)})
                written += 1
                log(f"  記入: {category} {ev_dt.date()} -> {res}")
            except Exception as e:
                log(f"  書き込み失敗 {category} {ev_dt.date()}: {type(e).__name__}: {e}")

    mode = "dry-run" if args.dry_run else "実行"
    log(f"fetch_results 完了({mode}): 対象={targets}, 記入={written}, "
        f"既記入スキップ={skipped_filled}, 未来スキップ={skipped_future}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
