"""
公式日程（米PCE / 米CPI / 全国CPI）を取得し、tmp/fetch_schedules_out.json に出力する。

fetch_fomc.py / fetch_boj.py と同じベストエフォート方式:
- 3系統それぞれ独立して try/except。1つ失敗しても他は続行する。
- 全滅しても return 0（後続 notion_upsert は build_events の出力で継続）。
- 取得できたものは is_estimated=False（公式確定情報）で出力する。

3系統:
  1. 米PCE  : BEA公式 https://www.bea.gov/news/schedule の
             "Personal Income and Outlays" 行を抽出。失敗時は BEA真値表をフォールバック。
  2. 米CPI  : BLS公式 https://www.bls.gov/schedule/news_release/cpi.htm を試す。
             BLSはbotブロック(403)になりやすいので、失敗時は BLS真値表をフォールバック。
  3. 全国CPI : 「対象月の翌月の、19日を含む週(月曜起算)の金曜 8:30 JST」のルールで
             プログラム計算（公式ページはJS依存で取りにくいためルール計算をプライマリ）。

出力フォーマット（build_events_out / fetch_fomc_out と同形式）:
    {"events": [ {...}, ... ], "fetched_at": "YYYY-MM-DDTHH:MM:SSZ"}

実行:
    python scripts/fetch_schedules.py
    python scripts/fetch_schedules.py --self-test   # Notion不要・取得と生成だけ実行して内容を表示
    python scripts/fetch_schedules.py --dry-run     # 件数のみ表示（tmp書き出しはする）
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import write_tmp, log, load_canonical_events  # noqa: E402


# ----------------------------------------------------------------------
# 定数
# ----------------------------------------------------------------------
USER_AGENT = "investor-calendar-bot/1.0 (+https://github.com/stomach04-beep/investor-calendar)"

BEA_URL = "https://www.bea.gov/news/schedule"
BLS_CPI_URL = "https://www.bls.gov/schedule/news_release/cpi.htm"
BEA_SOURCE_URL = "https://www.bea.gov/data/personal-consumption-expenditures-price-index"
BLS_SOURCE_URL = "https://www.bls.gov/cpi/"
JP_CPI_SOURCE_URL = "https://www.stat.go.jp/data/cpi/"

TZ_JST = ZoneInfo("Asia/Tokyo")
TZ_ET = ZoneInfo("America/New_York")

# 英語の月名 → 月番号
EN_MONTH_MAP = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
    "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# 月番号 → 日本語ラベル（title 用）
JP_MONTH_LABEL = {
    1: "1月", 2: "2月", 3: "3月", 4: "4月", 5: "5月", 6: "6月",
    7: "7月", 8: "8月", 9: "9月", 10: "10月", 11: "11月", 12: "12月",
}


# ----------------------------------------------------------------------
# シードの id エイリアス map
#   既存シードの CPI/PCE は (category, country, 対象年月) でユニーク（検証済み）。
#   公表日が公式値とズレている月でも、id だけはシードの既存値を再利用することで
#   notion_upsert が「新規ページ作成」ではなく「既存ページの上書き」になるようにする。
#   （表示は datetime_local/utc 基準なので、id と日付がズレても表示は正しい。
#    教訓: US CPI でも id と local日付のズレは許容するクリーンアップ方針）
# ----------------------------------------------------------------------
def _seed_target_ym(ev: dict) -> tuple[int, int] | None:
    """シードイベントの title から対象(年, 月)を復元する。"""
    t = ev.get("title", "")
    # "(2025年12月分)" 形式（年号付き）
    m = re.search(r"\((\d{4})年(\d{1,2})月分\)", t)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    # "(X月分)" 形式（年号なし）→ id の公表年から対象年を推定
    m = re.search(r"\((\d{1,2})月分\)", t)
    if m:
        tgt_m = int(m.group(1))
        ymd = ev["id"].split("_")[-1]
        pub_y, pub_m = int(ymd[:4]), int(ymd[5:7])
        tgt_y = pub_y if tgt_m <= pub_m else pub_y - 1
        return (tgt_y, tgt_m)
    return None


def load_seed_id_alias() -> dict[tuple[str, str, int, int], str]:
    """
    シードJSON の CPI/PCE を (category, country, 対象年, 対象月) → id の辞書にして返す。
    取得失敗時は空辞書（フォールバックで日付ベースの新規 id を使う）。
    """
    alias: dict[tuple[str, str, int, int], str] = {}
    try:
        data = load_canonical_events()
    except Exception as e:
        log(f"  fetch_schedules: シードid読込失敗 ({type(e).__name__}: {e}) → id エイリアスなしで続行")
        return alias
    for ev in data.get("events", []):
        if ev.get("category") not in ("CPI", "PCE"):
            continue
        ym = _seed_target_ym(ev)
        if ym is None:
            continue
        alias[(ev["category"], ev.get("country", ""), ym[0], ym[1])] = ev["id"]
    return alias


# モジュールロード時に1回だけ構築（各 make_* から参照）
_SEED_ID_ALIAS: dict[tuple[str, str, int, int], str] = {}


# ----------------------------------------------------------------------
# BEA / BLS 真値表（2026年・裏取り済み。フォールバック用）
# キー = 対象月の (year, month)、値 = 公表日(date)
# ----------------------------------------------------------------------
US_PCE_TRUTH: dict[tuple[int, int], date] = {
    (2026, 5): date(2026, 6, 25),    # 5月分 → 2026-06-25(木)
    (2026, 6): date(2026, 7, 30),    # 6月分 → 2026-07-30(木)
    (2026, 7): date(2026, 8, 26),    # 7月分 → 2026-08-26(水)
    (2026, 8): date(2026, 9, 30),    # 8月分 → 2026-09-30(水)
    (2026, 11): date(2026, 12, 23),  # 11月分 → 2026-12-23(水)
}

US_CPI_TRUTH: dict[tuple[int, int], date] = {
    (2025, 12): date(2026, 1, 13),   # 12月分(2025) → 2026-01-13(火)
    (2026, 1): date(2026, 2, 13),    # 1月分 → 2026-02-13(金)
    (2026, 2): date(2026, 3, 11),    # 2月分 → 2026-03-11(水)
    (2026, 3): date(2026, 4, 10),    # 3月分 → 2026-04-10(金)
    (2026, 4): date(2026, 5, 12),    # 4月分 → 2026-05-12(火)
    (2026, 5): date(2026, 6, 10),    # 5月分 → 2026-06-10(水)
    (2026, 6): date(2026, 7, 14),    # 6月分 → 2026-07-14(火)
}


# ----------------------------------------------------------------------
# 共通ユーティリティ
# ----------------------------------------------------------------------
def et_offset_str(d: date, hour: int = 8, minute: int = 30) -> str:
    """指定日の ET(America/New_York) のUTCオフセット文字列（例 '-04:00'）を返す。"""
    aware = datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ_ET)
    off = aware.utcoffset()
    assert off is not None
    total_min = int(off.total_seconds() // 60)
    sign = "+" if total_min >= 0 else "-"
    total_min = abs(total_min)
    return f"{sign}{total_min // 60:02d}:{total_min % 60:02d}"


def to_utc_z(local_iso: str) -> str:
    """オフセット付き ISO 文字列を UTC の 'YYYY-MM-DDTHH:MM:SSZ' に変換する。"""
    aware = datetime.fromisoformat(local_iso)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_us_event(category: str, title_prefix: str, target_month: int,
                  pub_date: date, source_url: str, target_year: int) -> dict:
    """米国指標（8:30 ET）の event dict を作る。datetime_utc は offset から再計算。"""
    offset = et_offset_str(pub_date, 8, 30)
    datetime_local = f"{pub_date.strftime('%Y-%m-%d')}T08:30:00{offset}"
    datetime_utc = to_utc_z(datetime_local)
    prefix = "us_pce" if category == "PCE" else "us_cpi"
    # シードに同じ対象年月のエントリーがあれば id を再利用（上書き＝重複防止）。
    # 無ければ公表日ベースの新規 id を使う。
    ev_id = _SEED_ID_ALIAS.get(
        (category, "US", target_year, target_month),
        f"{prefix}_{pub_date.strftime('%Y-%m-%d')}",
    )
    # title の対象月ラベル（前年12月分など、対象年が公表年と異なる場合は年号付き）
    if target_year != pub_date.year:
        title = f"{title_prefix} ({target_year}年{JP_MONTH_LABEL[target_month]}分)"
    else:
        title = f"{title_prefix} ({JP_MONTH_LABEL[target_month]}分)"
    desc = (
        "個人消費支出物価指数。FRBが最重視するインフレ指標。"
        if category == "PCE"
        else "消費者物価指数。FRB利下げ織り込みに直結。"
    )
    importance = 2 if category == "PCE" else 3
    return {
        "id": ev_id,
        "title": title,
        "category": category,
        "country": "US",
        "datetime_utc": datetime_utc,
        "datetime_local": datetime_local,
        "timezone": "America/New_York",
        "importance": importance,
        "is_estimated": False,
        "description": desc,
        "source_url": source_url,
        "result": None,
    }


# ----------------------------------------------------------------------
# 1. 米PCE: BEA公式から取得（フォールバック = 真値表）
# ----------------------------------------------------------------------
def fetch_bea_pce() -> list[dict]:
    """BEA公式スケジュールから Personal Income and Outlays の公表日を抽出する。"""
    events: list[dict] = []
    try:
        r = requests.get(BEA_URL, timeout=30, headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            log(f"  fetch_schedules[PCE]: BEA HTTP {r.status_code} → 真値表フォールバック")
            return pce_fallback()
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if table is None:
            log("  fetch_schedules[PCE]: BEAにテーブルなし → 真値表フォールバック")
            return pce_fallback()

        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            release_cell, _type_cell, desc_cell = cells[0], cells[1], cells[2]
            if "Personal Income and Outlays" not in desc_cell:
                continue
            # 対象月(year,month) を desc から抽出： "Personal Income and Outlays, May 2026"
            m_tgt = re.search(r",\s*([A-Za-z]+)\s+(\d{4})", desc_cell)
            if not m_tgt:
                continue
            tgt_month = EN_MONTH_MAP.get(m_tgt.group(1).lower())
            tgt_year = int(m_tgt.group(2))
            if not tgt_month:
                continue
            # 公表日(月名 + 日) を release_cell から抽出： "June 25 8:30 AM"
            m_rel = re.search(r"([A-Za-z]+)\s+(\d{1,2})", release_cell)
            if not m_rel:
                continue
            rel_month = EN_MONTH_MAP.get(m_rel.group(1).lower())
            rel_day = int(m_rel.group(2))
            if not rel_month:
                continue
            # 公表年を推定：公表は対象月の概ね翌月。公表月 < 対象月 なら年跨ぎ（翌年公表）。
            pub_year = tgt_year if rel_month >= tgt_month else tgt_year + 1
            try:
                pub_date = date(pub_year, rel_month, rel_day)
            except ValueError:
                continue
            events.append(make_us_event("PCE", "米PCEデフレーター", tgt_month, pub_date, BEA_SOURCE_URL, tgt_year))

        if not events:
            log("  fetch_schedules[PCE]: BEAから抽出ゼロ → 真値表フォールバック")
            return pce_fallback()
        log(f"  fetch_schedules[PCE]: BEA公式から {len(events)} 件取得")
        return events
    except Exception as e:
        log(f"  fetch_schedules[PCE]: BEA取得失敗 ({type(e).__name__}: {e}) → 真値表フォールバック")
        return pce_fallback()


def pce_fallback() -> list[dict]:
    """米PCE のフォールバック（BEA真値表からイベント生成）。"""
    out: list[dict] = []
    for (tgt_y, tgt_m), pub_date in US_PCE_TRUTH.items():
        out.append(make_us_event("PCE", "米PCEデフレーター", tgt_m, pub_date, BEA_SOURCE_URL, tgt_y))
    log(f"  fetch_schedules[PCE]: 真値表フォールバックで {len(out)} 件生成")
    return out


# ----------------------------------------------------------------------
# 2. 米CPI: BLS公式を試す（フォールバック = 真値表）
# ----------------------------------------------------------------------
def fetch_bls_cpi() -> list[dict]:
    """BLS公式スケジュールから CPI 公表日を抽出する。403が多いので失敗時は真値表。"""
    try:
        r = requests.get(BLS_CPI_URL, timeout=30, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        })
        if r.status_code != 200:
            log(f"  fetch_schedules[CPI]: BLS HTTP {r.status_code}（botブロックの想定内）→ 真値表フォールバック")
            return cpi_fallback()
        # BLSのCPIスケジュール表： "Reference Month | Release Date" 形式
        soup = BeautifulSoup(r.text, "html.parser")
        events: list[dict] = []
        for tr in soup.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            # 対象月セルと公表日セルを推定（"December 2025" / "Jan. 13, 2026" 等）
            ref_text = cells[0]
            rel_text = cells[-1]
            m_ref = re.search(r"([A-Za-z]+)\.?\s+(\d{4})", ref_text)
            m_rel = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", rel_text)
            if not m_ref or not m_rel:
                continue
            tgt_month = EN_MONTH_MAP.get(m_ref.group(1).lower())
            tgt_year = int(m_ref.group(2))
            rel_month = EN_MONTH_MAP.get(m_rel.group(1).lower())
            rel_day = int(m_rel.group(2))
            rel_year = int(m_rel.group(3))
            if not tgt_month or not rel_month:
                continue
            try:
                pub_date = date(rel_year, rel_month, rel_day)
            except ValueError:
                continue
            events.append(make_us_event("CPI", "米CPI", tgt_month, pub_date, BLS_SOURCE_URL, tgt_year))
        if not events:
            log("  fetch_schedules[CPI]: BLSから抽出ゼロ → 真値表フォールバック")
            return cpi_fallback()
        log(f"  fetch_schedules[CPI]: BLS公式から {len(events)} 件取得")
        return events
    except Exception as e:
        log(f"  fetch_schedules[CPI]: BLS取得失敗 ({type(e).__name__}: {e}) → 真値表フォールバック")
        return cpi_fallback()


def cpi_fallback() -> list[dict]:
    """米CPI のフォールバック（BLS真値表からイベント生成）。"""
    out: list[dict] = []
    for (tgt_y, tgt_m), pub_date in US_CPI_TRUTH.items():
        out.append(make_us_event("CPI", "米CPI", tgt_m, pub_date, BLS_SOURCE_URL, tgt_y))
    log(f"  fetch_schedules[CPI]: 真値表フォールバックで {len(out)} 件生成")
    return out


# ----------------------------------------------------------------------
# 3. 全国CPI(JP): ルール計算をプライマリにする
# ----------------------------------------------------------------------
def jp_cpi_publish_date(target_year: int, target_month: int) -> date:
    """
    全国CPI（総務省）の公表日を計算する。
    ルール: 対象月の「翌月」の、19日を含む週(月曜起算 Mon〜Sun)の金曜日。
    """
    pub_year = target_year
    pub_month = target_month + 1
    if pub_month > 12:
        pub_month = 1
        pub_year += 1
    d19 = date(pub_year, pub_month, 19)
    monday = d19 - timedelta(days=d19.weekday())  # weekday: Mon=0..Sun=6
    return monday + timedelta(days=4)  # 金曜


def build_jp_cpi(target_years: set[int]) -> list[dict]:
    """
    対象年の全国CPI（各月分）をルール計算で生成する。

    target_years は「公表年」基準でフィルタする（build_events / 既存シードの id が公表日基準のため）。
    対象月は前年12月分〜当年11月分までを候補にし、公表日の年が target_years に入るものを採用。
    """
    events: list[dict] = []
    for pub_year in sorted(target_years):
        # 公表年 pub_year に公表される対象月＝前年12月分〜当年11月分
        candidates: list[tuple[int, int]] = [(pub_year - 1, 12)] + [(pub_year, m) for m in range(1, 12)]
        for tgt_year, tgt_month in candidates:
            pub_date = jp_cpi_publish_date(tgt_year, tgt_month)
            if pub_date.year not in target_years:
                continue
            # 8:30 JST 固定
            datetime_local = f"{pub_date.strftime('%Y-%m-%d')}T08:30:00+09:00"
            datetime_utc = to_utc_z(datetime_local)
            # シードに同じ対象年月のエントリーがあれば id を再利用（上書き＝重複防止）
            ev_id = _SEED_ID_ALIAS.get(
                ("CPI", "JP", tgt_year, tgt_month),
                f"jp_cpi_{pub_date.strftime('%Y-%m-%d')}",
            )
            # title の対象月ラベル（前年12月分は年号付き）
            if tgt_year != pub_year:
                month_label = f"{tgt_year}年{JP_MONTH_LABEL[tgt_month]}"
            else:
                month_label = JP_MONTH_LABEL[tgt_month]
            events.append({
                "id": ev_id,
                "title": f"全国CPI ({month_label}分)",
                "category": "CPI",
                "country": "JP",
                "datetime_utc": datetime_utc,
                "datetime_local": datetime_local,
                "timezone": "Asia/Tokyo",
                "importance": 2,
                "is_estimated": False,
                "description": "総務省統計局発表。日銀の利上げ判断材料。",
                "source_url": JP_CPI_SOURCE_URL,
                "result": None,
            })
    log(f"  fetch_schedules[JP CPI]: ルール計算で {len(events)} 件生成")
    return events


# ----------------------------------------------------------------------
# 対象年フィルタ（fetch_fomc.py の load_target_years を踏襲）
# ----------------------------------------------------------------------
def load_target_years() -> set[int]:
    """build_events_out.json の covered_years を読み込む。なければ今年と来年。"""
    try:
        from common import load_tmp  # noqa: E402
        data = load_tmp("build_events_out")
        years = set(int(y) for y in data.get("covered_years", []))
        if years:
            return years
    except Exception:
        pass
    now_year = datetime.now(timezone.utc).year
    return {now_year, now_year + 1}


def event_year(ev: dict) -> int:
    """event の datetime_local の年を返す（フィルタ用）。"""
    return datetime.fromisoformat(ev["datetime_local"]).year


# ----------------------------------------------------------------------
# 生成本体（self-test からも呼べるよう main から分離）
# ----------------------------------------------------------------------
def collect_schedule_events() -> list[dict]:
    """3系統を独立実行してマージした events を返す（target_years でフィルタ済み）。"""
    global _SEED_ID_ALIAS
    # シードの id エイリアスを構築（公式日付がズレても既存ページを上書きするため）
    _SEED_ID_ALIAS = load_seed_id_alias()
    log(f"  fetch_schedules: シードid エイリアス {len(_SEED_ID_ALIAS)} 件読込")

    target_years = load_target_years()
    log(f"  fetch_schedules: target_years={sorted(target_years)}")

    all_events: list[dict] = []
    # 1. 米PCE（独立try/except）
    try:
        all_events.extend(fetch_bea_pce())
    except Exception as e:
        log(f"  fetch_schedules[PCE]: 想定外の失敗 ({type(e).__name__}: {e}) → スキップ")
    # 2. 米CPI（独立try/except）
    try:
        all_events.extend(fetch_bls_cpi())
    except Exception as e:
        log(f"  fetch_schedules[CPI]: 想定外の失敗 ({type(e).__name__}: {e}) → スキップ")
    # 3. 全国CPI（独立try/except、ルール計算）
    try:
        all_events.extend(build_jp_cpi(target_years))
    except Exception as e:
        log(f"  fetch_schedules[JP CPI]: 想定外の失敗 ({type(e).__name__}: {e}) → スキップ")

    # target_years でフィルタ（datetime_local の年で判定）
    filtered = [e for e in all_events if event_year(e) in target_years]
    # id 重複は後勝ちで除去
    by_id: dict[str, dict] = {}
    for e in filtered:
        by_id[e["id"]] = e
    return sorted(by_id.values(), key=lambda e: e["datetime_utc"])


# ----------------------------------------------------------------------
# メイン
# ----------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="公式日程(米PCE/米CPI/全国CPI)を取得して tmp に出力")
    parser.add_argument("--self-test", action="store_true",
                        help="Notion不要・取得と生成だけ実行して内容を表示（tmpにも書く）")
    parser.add_argument("--dry-run", action="store_true",
                        help="件数のみ表示（tmpには書き出す）")
    args = parser.parse_args()

    events = collect_schedule_events()
    if not events:
        log("fetch_schedules: 取得・生成できたイベントなし → 出力ファイルなし（後続は build_events 出力で継続）")
        return 0

    # tmp 書き出し（self-test / dry-run でも書く＝検証で中身を見られるように）
    path = write_tmp("fetch_schedules_out", {
        "events": events,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    log(f"fetch_schedules: 全 {len(events)} 件を {path} に書き出し")

    if args.self_test or args.dry_run:
        # カテゴリ別件数
        from collections import Counter
        cnt = Counter((e["category"], e["country"]) for e in events)
        log("  カテゴリ別件数:")
        for (cat, country), n in sorted(cnt.items()):
            log(f"    {cat}/{country}: {n} 件")
        if args.self_test:
            log("  --- self-test: 生成イベント一覧（id | local | utc）---")
            for e in events:
                log(f"    {e['id']} | {e['datetime_local']} | {e['datetime_utc']} | {e['title']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
