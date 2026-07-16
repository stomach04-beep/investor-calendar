"""
公式日程（米PCE / 米CPI / 全国CPI / 米雇用統計 / 米PPI / 米GDP / 米小売売上高 / ISM）を取得し、
tmp/fetch_schedules_out.json に出力する。

fetch_fomc.py / fetch_boj.py と同じベストエフォート方式:
- 各系統それぞれ独立して try/except。1つ失敗しても他は続行する。
- 全滅しても return 0（後続 notion_upsert は build_events の出力で継続）。
- 取得できたものは原則 is_estimated=False（公式確定情報）で出力する。

5系統:
  1. 米PCE     : BEA公式 https://www.bea.gov/news/schedule の
                "Personal Income and Outlays" 行を抽出。失敗時は BEA真値表をフォールバック。
  2. 米CPI     : BLS公式 https://www.bls.gov/schedule/news_release/cpi.htm を試す。
                ただし BLS は Akamai の bot 遮断で「恒常的に」403（2026-07-15 実測：
                User-Agent をブラウザ偽装しても、bls.ics でも、GitHub Actions からも全て403）。
                つまり真値表フォールバックが実質の本番経路であり、真値表の鮮度が生命線。
                切れると誤日付が黙って残るため warn_uncovered_targets() で警告する。
  3. 全国CPI   : 「対象月の翌月の、19日を含む週(月曜起算)の金曜 8:30 JST」のルールで
                プログラム計算（公式ページはJS依存で取りにくいためルール計算をプライマリ）。
  4. 米雇用統計 : 「毎月第1金曜 8:30 ET」をルール計算（例外月＝祝日前倒し等は上書き表）。
                2026-07は7/3(独立記念日振替)のため7/2(木)に前倒し。
                対象年の12月分は翌年1月公表なので、公表月ループは翌年1月まで伸ばす。
  5. 米PPI     : BLS公式 https://www.bls.gov/schedule/news_release/ppi.htm を試す（CPI同様に恒常403）。
                真値表を優先し、表に無い月のみ「対応CPIの翌営業日」で近似する。
                ※この近似は PPI が CPI より先に出る月（2026年8月分＝PPI 9/10 < CPI 9/11）を
                  構造的に表現できない。あくまで最後の手段であり、真値表を埋めるのが正。
  6. 米GDP     : BEA公式 https://www.bea.gov/news/schedule の
                "Gross Domestic Product" 行を抽出（速報値=Advance / 改定値=Second のみ。
                確報値=Third は相場インパクトが小さいので載せない）。失敗時は真値表。
  7. 米小売売上高: Census公式 https://www.census.gov/retail/release_schedule.html を試す。
                失敗時は真値表（2026年公式日程・裏取り済み）フォールバック。
  8. ISM景況指数: ルール計算。製造業=毎月第1営業日（1月のみ第2営業日）、
                非製造業=毎月第3営業日、いずれも 10:00 ET（ISM公式ルール）。
                公式カレンダーが取れないため is_estimated=True で近似する。

重要（Notion重複防止）:
  CPI/PCE/JOBS/PPI は、シードJSON の既存 id を (category,country,対象年,対象月) で
  再利用する（ALIAS_CATEGORIES）。公式日付がズレても id は不変識別子として据え置き、
  notion_upsert が「新規作成」ではなく「既存ページ上書き」になるようにする。

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
from common import write_tmp, log, load_canonical_events, record_fetch_warning  # noqa: E402


# ----------------------------------------------------------------------
# 定数
# ----------------------------------------------------------------------
USER_AGENT = "investor-calendar-bot/1.0 (+https://github.com/stomach04-beep/investor-calendar)"

BEA_URL = "https://www.bea.gov/news/schedule"
BLS_CPI_URL = "https://www.bls.gov/schedule/news_release/cpi.htm"
BEA_SOURCE_URL = "https://www.bea.gov/data/personal-consumption-expenditures-price-index"
BLS_SOURCE_URL = "https://www.bls.gov/cpi/"
JP_CPI_SOURCE_URL = "https://www.stat.go.jp/data/cpi/"
# 米雇用統計のソースURL。シードに BEA(PCE) の URL が紛れ込んでいた事故があるため、
# 生成側の単一定義から必ず埋める（DRY: 表示・派生側で再定義しない）。
JOBS_SOURCE_URL = "https://www.bls.gov/schedule/news_release/empsit.htm"

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


# id を再利用するカテゴリ（シードの既存 id を使う＝Notion重複防止の要）
#   CPI/PCE に加え、JOBS(雇用統計)/PPI もシードidを再利用する。
#   公式日付がズレても id だけは不変識別子として据え置く（教訓: 日付ベースidは不変）。
ALIAS_CATEGORIES = ("CPI", "PCE", "JOBS", "PPI")


def load_seed_id_alias() -> dict[tuple[str, str, int, int], str]:
    """
    シードJSON の CPI/PCE/JOBS/PPI を (category, country, 対象年, 対象月) → id の辞書にして返す。
    取得失敗時は空辞書（フォールバックで日付ベースの新規 id を使う）。
    """
    alias: dict[tuple[str, str, int, int], str] = {}
    try:
        data = load_canonical_events()
    except Exception as e:
        log(f"  fetch_schedules: シードid読込失敗 ({type(e).__name__}: {e}) → id エイリアスなしで続行")
        return alias
    for ev in data.get("events", []):
        if ev.get("category") not in ALIAS_CATEGORIES:
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
    # 以下は 2026-07-15 に BLS公式(cpi.htm)で裏取りして追加。
    # ここが 6月分で途切れていたため、7月分以降はフォールバックすら生成されず
    # シードJSONの誤日付がそのまま素通りしていた（本バグの直接原因）。
    (2026, 7): date(2026, 8, 12),    # 7月分 → 2026-08-12(水)
    (2026, 8): date(2026, 9, 11),    # 8月分 → 2026-09-11(金)
    (2026, 9): date(2026, 10, 14),   # 9月分 → 2026-10-14(水)
    (2026, 10): date(2026, 11, 10),  # 10月分 → 2026-11-10(火)
    (2026, 11): date(2026, 12, 10),  # 11月分 → 2026-12-10(木)
}

# ----------------------------------------------------------------------
# 雇用統計(JOBS)の例外公表日
#   原則「毎月第1金曜 8:30 ET」だが、祝日等で前倒しになる月だけ上書きする。
#   キー = 公表年月(year, month)、値 = 実際の公表日。
#   2026-07: 7/3(金)が独立記念日の振替休のため 7/2(木)に前倒し。
# ----------------------------------------------------------------------
US_JOBS_PUBLISH_OVERRIDE: dict[tuple[int, int], date] = {
    (2026, 7): date(2026, 7, 2),
    # 2027-01: 第1金曜が 1/1(元日=祝日)。雇用統計は集計が間に合わないため
    # 前倒しではなく翌週金曜へ後ろ倒しになる（2021-01 も同型で 1/8 公表）。
    # ※ 7月の独立記念日は「前倒し」(7/3休→7/2)、元日は「後ろ倒し」で向きが逆。
    #    規則化すると必ず取り違えるので、例外は個別に裏取りして表で持つ。
    (2027, 1): date(2027, 1, 8),
}

# BLS が公式スケジュールを公表済みの範囲（2026-07-15 時点で empsit.htm は 11月分＝12/4 まで）。
# これ以降の公表日はルール計算・慣例による推定なので is_estimated=True にする。
# 確定扱い(False)にすると notion_upsert の保護対象になり、BLS が2027年分を公表しても
# 二度と更新されなくなるため（＝間違ったまま固着する）。
US_JOBS_CONFIRMED_THROUGH = date(2026, 12, 4)

# ----------------------------------------------------------------------
# 米PPI 真値表（2026年・裏取り済み。フォールバック用）
#   確定公表日（is_estimated=False）。キー = 対象月(year, month)、値 = 公表日。
#   ここに無い対象月は「対応する米CPIの公表日の翌営業日」を近似(is_estimated=True)で置く。
# ----------------------------------------------------------------------
US_PPI_TRUTH: dict[tuple[int, int], date] = {
    (2026, 1): date(2026, 2, 27),    # 1月分 → 2026-02-27(金)
    (2026, 3): date(2026, 4, 14),    # 3月分 → 2026-04-14(火)
    (2026, 4): date(2026, 5, 13),    # 4月分 → 2026-05-13(水)
    (2026, 5): date(2026, 6, 11),    # 5月分 → 2026-06-11(木)
    (2026, 6): date(2026, 7, 15),    # 6月分 → 2026-07-15(水)
    # 以下は 2026-07-15 に BLS公式(ppi.htm)で裏取りして追加。
    # 「CPI公表日の翌営業日」近似は PPI が CPI より先に出る月（8月分=9/10 < CPI 9/11 等）を
    # 構造的に表現できず、5件中4件が誤りだった。公式日程で置き換える。
    (2026, 7): date(2026, 8, 13),    # 7月分 → 2026-08-13(木)
    (2026, 8): date(2026, 9, 10),    # 8月分 → 2026-09-10(木)
    (2026, 9): date(2026, 10, 15),   # 9月分 → 2026-10-15(木)
    (2026, 10): date(2026, 11, 13),  # 10月分 → 2026-11-13(金)
    (2026, 11): date(2026, 12, 15),  # 11月分 → 2026-12-15(火)
}

# 米PPI 公式スケジュールURL（403が多いので失敗時は真値表＋CPI翌営業日近似にフォールバック）
BLS_PPI_URL = "https://www.bls.gov/schedule/news_release/ppi.htm"
BLS_PPI_SOURCE_URL = "https://www.bls.gov/ppi/"

# ----------------------------------------------------------------------
# 米GDP 真値表（2026年・BEA公式 https://www.bea.gov/news/schedule で裏取り済み 2026-07-08）
#   キー = (対象年, 四半期, 種別)。種別 "adv"=速報値(Advance) / "2nd"=改定値(Second)。
#   確報値(Third)は相場インパクトが小さいのでカレンダーに載せない。
# ----------------------------------------------------------------------
US_GDP_TRUTH: dict[tuple[int, int, str], date] = {
    (2026, 2, "adv"): date(2026, 7, 30),    # Q2速報 → 2026-07-30(木)
    (2026, 2, "2nd"): date(2026, 8, 26),    # Q2改定 → 2026-08-26(水)
    (2026, 3, "adv"): date(2026, 10, 29),   # Q3速報 → 2026-10-29(木)
    (2026, 3, "2nd"): date(2026, 11, 25),   # Q3改定 → 2026-11-25(水)
}
BEA_GDP_SOURCE_URL = "https://www.bea.gov/data/gdp/gross-domestic-product"

# ----------------------------------------------------------------------
# 米小売売上高 真値表（2026年・Census公式 release_schedule.html で裏取り済み 2026-07-08）
#   キー = 対象月の (year, month)、値 = 公表日（8:30 ET）
# ----------------------------------------------------------------------
US_RETAIL_TRUTH: dict[tuple[int, int], date] = {
    (2026, 4): date(2026, 5, 14),    # 4月分 → 2026-05-14(木)
    (2026, 5): date(2026, 6, 17),    # 5月分 → 2026-06-17(水)
    (2026, 6): date(2026, 7, 16),    # 6月分 → 2026-07-16(木)
    (2026, 7): date(2026, 8, 14),    # 7月分 → 2026-08-14(金)
    (2026, 8): date(2026, 9, 16),    # 8月分 → 2026-09-16(水)
    (2026, 9): date(2026, 10, 15),   # 9月分 → 2026-10-15(木)
    (2026, 10): date(2026, 11, 17),  # 10月分 → 2026-11-17(火)
    (2026, 11): date(2026, 12, 16),  # 11月分 → 2026-12-16(水)
}
CENSUS_RETAIL_URL = "https://www.census.gov/retail/release_schedule.html"
CENSUS_RETAIL_SOURCE_URL = "https://www.census.gov/retail/"

# ISM 公式レポートページ（日程はルール計算するのでソースURL表示用のみ）
ISM_SOURCE_URL = "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/"


# ----------------------------------------------------------------------
# 共通ユーティリティ
# ----------------------------------------------------------------------
def warn(msg: str) -> None:
    """
    GitHub Actions のアノテーション付き警告を出す（実行サマリーに赤く出る）。
    ローカル実行でもただの行として読める。
    """
    log(f"::warning::{msg}")


def warn_uncovered_targets(label: str, category: str, country: str,
                           truth: dict[tuple[int, int], date],
                           today: date | None = None) -> None:
    """
    シードに存在する対象月のうち、真値表に無いものを警告する。

    真値表が切れている対象月は fetch_schedules が何も生成しないため、シードJSONの
    古い日付がそのまま Notion に残り続ける（notion_to_json がシードに書き戻すので
    誤りが自己増殖する）。ジョブは success のままなので気付けない。
    2026-07 に CPI/PPI で実際にこれが起きたので、切れたら必ず声を上げる。

    公表済みの過去月は今さら直しても意味がなく、毎朝鳴り続けると警告全体が
    無視されるようになるため、当月以降（＝まだ直せる分）だけを対象にする。
    """
    today = today or datetime.now(TZ_ET).date()
    missing = sorted(
        (y, m)
        for (cat, ctry, y, m) in _SEED_ID_ALIAS
        if cat == category and ctry == country
        and (y, m) not in truth
        and (y, m) >= (today.year, today.month)
    )
    if missing:
        ym_txt = ", ".join(f"{y}-{m:02d}" for y, m in missing)
        warn(
            f"{label}: 真値表に無い対象月があります（{ym_txt}）。"
            f"この月はシードJSONの古い日付が上書きされずに残ります。"
            f"BLS公式スケジュールを確認して真値表を追加してください。"
        )


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
    record_fetch_warning("fetch_schedules[PCE]", f"BEA公式から取得できず真値表フォールバック使用（{len(out)} 件）")
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
    record_fetch_warning("fetch_schedules[CPI]", f"BLS公式から取得できず真値表フォールバック使用（{len(out)} 件）")
    warn_uncovered_targets("fetch_schedules[CPI]", "CPI", "US", US_CPI_TRUTH)
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
# 4. 米雇用統計(JOBS): ルール計算（毎月第1金曜 8:30 ET、例外月は上書き）
# ----------------------------------------------------------------------
def first_friday(year: int, month: int) -> date:
    """指定年月の第1金曜日を返す。"""
    d1 = date(year, month, 1)
    # weekday: Mon=0..Sun=6, 金=4。1日から最初の金曜までの日数を足す。
    offset = (4 - d1.weekday()) % 7
    return d1 + timedelta(days=offset)


def jobs_publish_date(pub_year: int, pub_month: int) -> date:
    """
    雇用統計の公表日を返す。
    原則は「公表月の第1金曜 8:30 ET」。例外月（祝日前倒し等）は上書き表を優先。
    """
    override = US_JOBS_PUBLISH_OVERRIDE.get((pub_year, pub_month))
    if override is not None:
        return override
    return first_friday(pub_year, pub_month)


def make_jobs_event(target_year: int, target_month: int, pub_date: date) -> dict:
    """米雇用統計（8:30 ET）の event dict を作る。id はシードの us_nfp_* を再利用。"""
    offset = et_offset_str(pub_date, 8, 30)
    datetime_local = f"{pub_date.strftime('%Y-%m-%d')}T08:30:00{offset}"
    datetime_utc = to_utc_z(datetime_local)
    # シードに同じ対象年月のエントリーがあれば id を再利用（上書き＝重複防止）。
    ev_id = _SEED_ID_ALIAS.get(
        ("JOBS", "US", target_year, target_month),
        f"us_nfp_{pub_date.strftime('%Y-%m-%d')}",
    )
    # title の対象月ラベル（前年12月分など、対象年が公表年と異なる場合は年号付き）
    if target_year != pub_date.year:
        title = f"米雇用統計 ({target_year}年{JP_MONTH_LABEL[target_month]}分)"
    else:
        title = f"米雇用統計 ({JP_MONTH_LABEL[target_month]}分)"
    return {
        "id": ev_id,
        "title": title,
        "category": "JOBS",
        "country": "US",
        "datetime_utc": datetime_utc,
        "datetime_local": datetime_local,
        "timezone": "America/New_York",
        "importance": 3,
        # BLS公表済み範囲内なら確定、それ以降（2027年分など）は推定として追従を残す
        "is_estimated": pub_date > US_JOBS_CONFIRMED_THROUGH,
        "description": "非農業部門雇用者数(NFP)、失業率、平均時給。FRB政策の最重要指標。",
        "source_url": JOBS_SOURCE_URL,
        "result": None,
    }


def build_us_jobs(target_years: set[int]) -> list[dict]:
    """
    対象年の米雇用統計（各月分）をルール計算で生成する。

    公表年でループし、各月の第1金曜（例外月は上書き）を公表日とする。
    雇用統計の「X月分」は翌月公表なので、対象月 = 公表月の前月。

    年跨ぎ: 対象年の「12月分」は翌年1月公表なので、公表月ループを翌年1月まで伸ばす。
    ここを 1〜12月で閉じていたため 12月分が一度も生成されず、シードJSONに残った
    幽霊行（us_nfp_2026-12-31・ソースURLがBEA）が誰にも上書きされず固着していた。
    """
    events: list[dict] = []
    for pub_year in sorted(target_years):
        # 翌年1月＝対象年12月分の公表月。target_years に翌年が入っていれば
        # 重複するが、呼び出し元で id 一意化されるので無害。
        pub_months = [(pub_year, m) for m in range(1, 13)] + [(pub_year + 1, 1)]
        for py, pub_month in pub_months:
            pub_date = jobs_publish_date(py, pub_month)
            # 公表月の前月が対象月（1月公表 → 前年12月分）
            if pub_month == 1:
                tgt_year, tgt_month = py - 1, 12
            else:
                tgt_year, tgt_month = py, pub_month - 1
            events.append(make_jobs_event(tgt_year, tgt_month, pub_date))
    log(f"  fetch_schedules[JOBS]: ルール計算で {len(events)} 件生成")
    return events


# ----------------------------------------------------------------------
# 5. 米PPI: BLS公式を試す（フォールバック = 真値表＋CPI翌営業日近似）
# ----------------------------------------------------------------------
def next_business_day(d: date) -> date:
    """翌営業日（土日をスキップ）。祝日は考慮しない近似。"""
    nd = d + timedelta(days=1)
    while nd.weekday() >= 5:  # 5=土, 6=日
        nd += timedelta(days=1)
    return nd


def make_ppi_event(target_year: int, target_month: int, pub_date: date,
                   is_estimated: bool) -> dict:
    """米PPI（8:30 ET）の event dict を作る。id はシードの us_ppi_* を再利用。"""
    offset = et_offset_str(pub_date, 8, 30)
    datetime_local = f"{pub_date.strftime('%Y-%m-%d')}T08:30:00{offset}"
    datetime_utc = to_utc_z(datetime_local)
    ev_id = _SEED_ID_ALIAS.get(
        ("PPI", "US", target_year, target_month),
        f"us_ppi_{pub_date.strftime('%Y-%m-%d')}",
    )
    if target_year != pub_date.year:
        title = f"米PPI ({target_year}年{JP_MONTH_LABEL[target_month]}分)"
    else:
        title = f"米PPI ({JP_MONTH_LABEL[target_month]}分)"
    return {
        "id": ev_id,
        "title": title,
        "category": "PPI",
        "country": "US",
        "datetime_utc": datetime_utc,
        "datetime_local": datetime_local,
        "timezone": "America/New_York",
        "importance": 2,
        "is_estimated": is_estimated,
        "description": "生産者物価指数。卸売段階のインフレ先行指標。",
        "source_url": BLS_PPI_SOURCE_URL,
        "result": None,
    }


def _us_cpi_pubdate_by_target() -> dict[tuple[int, int], date]:
    """
    PPI未確定月の「CPI翌営業日」算出に使う、対象月→米CPI公表日 の辞書。
    まず US_CPI_TRUTH を採用。足りない分はシードの 米CPI(datetime_local) から補う。
    """
    out: dict[tuple[int, int], date] = dict(US_CPI_TRUTH)
    try:
        data = load_canonical_events()
    except Exception:
        return out
    for ev in data.get("events", []):
        if ev.get("category") != "CPI" or ev.get("country") != "US":
            continue
        ym = _seed_target_ym(ev)
        if ym is None:
            continue
        if ym not in out:  # 真値表優先、無い月だけシード値で補完
            out[ym] = datetime.fromisoformat(ev["datetime_local"]).date()
    return out


def ppi_fallback() -> list[dict]:
    """
    米PPI のフォールバック。
      - US_PPI_TRUTH の5件は確定（is_estimated=False）。
      - それ以外（対象月レンジ内）は「対応CPIの公表日の翌営業日」を近似（is_estimated=True）。
    対象月レンジ = 米CPIと同一（2025年12月分 〜 2026年11月分）。
    """
    target_months: list[tuple[int, int]] = [(2025, 12)] + [(2026, m) for m in range(1, 12)]
    cpi_map = _us_cpi_pubdate_by_target()
    out: list[dict] = []
    for (tgt_y, tgt_m) in target_months:
        if (tgt_y, tgt_m) in US_PPI_TRUTH:
            out.append(make_ppi_event(tgt_y, tgt_m, US_PPI_TRUTH[(tgt_y, tgt_m)], is_estimated=False))
        else:
            cpi_d = cpi_map.get((tgt_y, tgt_m))
            if cpi_d is None:
                log(f"  fetch_schedules[PPI]: 対象 {tgt_y}-{tgt_m} のCPI公表日不明 → スキップ")
                continue
            out.append(make_ppi_event(tgt_y, tgt_m, next_business_day(cpi_d), is_estimated=True))
    log(f"  fetch_schedules[PPI]: フォールバックで {len(out)} 件生成（確定 {len(US_PPI_TRUTH)} 件）")
    record_fetch_warning("fetch_schedules[PPI]", f"BLS公式から取得できず真値表＋CPI翌営業日近似フォールバック使用（{len(out)} 件）")
    warn_uncovered_targets("fetch_schedules[PPI]", "PPI", "US", US_PPI_TRUTH)
    return out


def fetch_bls_ppi() -> list[dict]:
    """BLS公式スケジュールから PPI 公表日を抽出する。403が多いので失敗時は真値表＋近似。"""
    try:
        r = requests.get(BLS_PPI_URL, timeout=30, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        })
        if r.status_code != 200:
            log(f"  fetch_schedules[PPI]: BLS HTTP {r.status_code}（botブロックの想定内）→ フォールバック")
            return ppi_fallback()
        soup = BeautifulSoup(r.text, "html.parser")
        events: list[dict] = []
        for tr in soup.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
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
            # 公式から取れたものは確定扱い
            events.append(make_ppi_event(tgt_year, tgt_month, pub_date, is_estimated=False))
        if not events:
            log("  fetch_schedules[PPI]: BLSから抽出ゼロ → フォールバック")
            return ppi_fallback()
        log(f"  fetch_schedules[PPI]: BLS公式から {len(events)} 件取得")
        return events
    except Exception as e:
        log(f"  fetch_schedules[PPI]: BLS取得失敗 ({type(e).__name__}: {e}) → フォールバック")
        return ppi_fallback()


# ----------------------------------------------------------------------
# 6. 米GDP: BEA公式から取得（フォールバック = 真値表）
#    速報値(Advance)と改定値(Second)のみ。id は対象四半期ベースで不変
#    （us_gdp_2026q2_adv 形式。公表日がズレても id は変わらない）。
# ----------------------------------------------------------------------
GDP_KIND_LABEL = {"adv": "速報値", "2nd": "改定値"}


def make_gdp_event(tgt_year: int, quarter: int, kind: str, pub_date: date,
                   is_estimated: bool = False) -> dict:
    """米GDP（8:30 ET）の event dict を作る。kind は 'adv'(速報) / '2nd'(改定)。"""
    offset = et_offset_str(pub_date, 8, 30)
    datetime_local = f"{pub_date.strftime('%Y-%m-%d')}T08:30:00{offset}"
    datetime_utc = to_utc_z(datetime_local)
    label = GDP_KIND_LABEL[kind]
    # 速報値は市場が動く（★★☆）、改定値は参考（★☆☆）
    importance = 2 if kind == "adv" else 1
    return {
        "id": f"us_gdp_{tgt_year}q{quarter}_{kind}",
        "title": f"米GDP{label} (Q{quarter})",
        "category": "GDP",
        "country": "US",
        "datetime_utc": datetime_utc,
        "datetime_local": datetime_local,
        "timezone": "America/New_York",
        "importance": importance,
        "is_estimated": is_estimated,
        "description": f"米実質GDP成長率（前期比年率）の{label}。"
                       "速報値は四半期終了の約1ヶ月後に公表され、サプライズで相場が動きやすい。",
        "source_url": BEA_GDP_SOURCE_URL,
        "result": None,
    }


def gdp_fallback() -> list[dict]:
    """米GDP のフォールバック（BEA真値表からイベント生成）。"""
    out = [make_gdp_event(y, q, kind, d) for (y, q, kind), d in US_GDP_TRUTH.items()]
    log(f"  fetch_schedules[GDP]: 真値表フォールバックで {len(out)} 件生成")
    record_fetch_warning("fetch_schedules[GDP]", f"BEA公式から取得できず真値表フォールバック使用（{len(out)} 件）")
    return out


def fetch_bea_gdp() -> list[dict]:
    """BEA公式スケジュールから Gross Domestic Product の公表日を抽出する。"""
    try:
        r = requests.get(BEA_URL, timeout=30, headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            log(f"  fetch_schedules[GDP]: BEA HTTP {r.status_code} → 真値表フォールバック")
            return gdp_fallback()
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if table is None:
            log("  fetch_schedules[GDP]: BEAにテーブルなし → 真値表フォールバック")
            return gdp_fallback()

        events: list[dict] = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            release_cell, _type_cell, desc_cell = cells[0], cells[1], cells[2]
            # BEA実ページの表記は "GDP (Advance Estimate), 2nd Quarter 2026" 形式
            # （"GDP by County..." 等の年次統計行は Quarter 正規表現で自然に除外される）
            if "GDP (" not in desc_cell and "Gross Domestic Product" not in desc_cell:
                continue
            # 種別: Advance=速報 / Second=改定。Third(確報)は載せない
            if "Advance" in desc_cell:
                kind = "adv"
            elif "Second" in desc_cell:
                kind = "2nd"
            else:
                continue
            # 対象四半期: "2nd Quarter 2026" 形式
            m_q = re.search(r"(\d)(?:st|nd|rd|th)\s+Quarter\s+(\d{4})", desc_cell)
            if not m_q:
                continue
            quarter = int(m_q.group(1))
            tgt_year = int(m_q.group(2))
            # 公表日: "July 30 8:30 AM" 形式
            m_rel = re.search(r"([A-Za-z]+)\s+(\d{1,2})", release_cell)
            if not m_rel:
                continue
            rel_month = EN_MONTH_MAP.get(m_rel.group(1).lower())
            rel_day = int(m_rel.group(2))
            if not rel_month:
                continue
            # 公表年の推定: 公表月が四半期末月より後なら同年、前なら翌年（Q4速報は翌年1月）
            quarter_end_month = quarter * 3
            pub_year = tgt_year if rel_month > quarter_end_month else tgt_year + 1
            try:
                pub_date = date(pub_year, rel_month, rel_day)
            except ValueError:
                continue
            events.append(make_gdp_event(tgt_year, quarter, kind, pub_date))

        if not events:
            log("  fetch_schedules[GDP]: BEAから抽出ゼロ → 真値表フォールバック")
            return gdp_fallback()
        log(f"  fetch_schedules[GDP]: BEA公式から {len(events)} 件取得")
        return events
    except Exception as e:
        log(f"  fetch_schedules[GDP]: BEA取得失敗 ({type(e).__name__}: {e}) → 真値表フォールバック")
        return gdp_fallback()


# ----------------------------------------------------------------------
# 7. 米小売売上高: Census公式から取得（フォールバック = 真値表）
#    id は対象月ベースで不変（us_retail_2026-06 形式）。
# ----------------------------------------------------------------------
def make_retail_event(tgt_year: int, tgt_month: int, pub_date: date,
                      is_estimated: bool = False) -> dict:
    """米小売売上高（8:30 ET）の event dict を作る。"""
    offset = et_offset_str(pub_date, 8, 30)
    datetime_local = f"{pub_date.strftime('%Y-%m-%d')}T08:30:00{offset}"
    datetime_utc = to_utc_z(datetime_local)
    if tgt_year != pub_date.year:
        title = f"米小売売上高 ({tgt_year}年{JP_MONTH_LABEL[tgt_month]}分)"
    else:
        title = f"米小売売上高 ({JP_MONTH_LABEL[tgt_month]}分)"
    return {
        "id": f"us_retail_{tgt_year}-{tgt_month:02d}",
        "title": title,
        "category": "RETAIL",
        "country": "US",
        "datetime_utc": datetime_utc,
        "datetime_local": datetime_local,
        "timezone": "America/New_York",
        "importance": 2,
        "is_estimated": is_estimated,
        "description": "米国の小売・飲食サービス売上高（前月比）。米経済の約7割を占める"
                       "個人消費の強さを測る速報。CPI級に相場が動く月もある。",
        "source_url": CENSUS_RETAIL_SOURCE_URL,
        "result": None,
    }


def retail_fallback() -> list[dict]:
    """米小売売上高のフォールバック（Census真値表からイベント生成）。"""
    out = [make_retail_event(y, m, d) for (y, m), d in US_RETAIL_TRUTH.items()]
    log(f"  fetch_schedules[RETAIL]: 真値表フォールバックで {len(out)} 件生成")
    record_fetch_warning("fetch_schedules[RETAIL]", f"Census公式から取得できず真値表フォールバック使用（{len(out)} 件）")
    return out


def fetch_census_retail() -> list[dict]:
    """Census公式 release_schedule.html から MARTS（速報）公表日を抽出する。"""
    try:
        r = requests.get(CENSUS_RETAIL_URL, timeout=30, headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            log(f"  fetch_schedules[RETAIL]: Census HTTP {r.status_code} → 真値表フォールバック")
            return retail_fallback()
        soup = BeautifulSoup(r.text, "html.parser")
        events: list[dict] = []
        for tr in soup.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            # 対象月セル "June 2026" / 公表日セル "July 16, 2026" を推定
            m_ref = re.search(r"([A-Za-z]+)\.?\s+(\d{4})", cells[0])
            m_rel = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", cells[1])
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
            events.append(make_retail_event(tgt_year, tgt_month, pub_date))
        if not events:
            log("  fetch_schedules[RETAIL]: Censusから抽出ゼロ → 真値表フォールバック")
            return retail_fallback()
        log(f"  fetch_schedules[RETAIL]: Census公式から {len(events)} 件取得")
        return events
    except Exception as e:
        log(f"  fetch_schedules[RETAIL]: Census取得失敗 ({type(e).__name__}: {e}) → 真値表フォールバック")
        return retail_fallback()


# ----------------------------------------------------------------------
# 8. ISM景況指数: ルール計算
#    製造業 = 毎月第1営業日（1月のみ第2営業日）、非製造業 = 毎月第3営業日、10:00 ET。
#    ISMの公式カレンダーは取得しにくいため is_estimated=True の近似で出す。
#    id は対象月ベースで不変（us_ism_mfg_2026-06 形式。対象月 = 公表月の前月）。
# ----------------------------------------------------------------------
def us_federal_holidays(year: int) -> set[date]:
    """営業日判定に使う米国の主要祝日（振替込み）。"""
    def observed(d: date) -> date:
        # 土曜→前日金曜、日曜→翌日月曜に振替
        if d.weekday() == 5:
            return d - timedelta(days=1)
        if d.weekday() == 6:
            return d + timedelta(days=1)
        return d

    def nth_weekday(month: int, weekday: int, n: int) -> date:
        d1 = date(year, month, 1)
        offset = (weekday - d1.weekday()) % 7
        return d1 + timedelta(days=offset + (n - 1) * 7)

    def last_weekday(month: int, weekday: int) -> date:
        # 翌月1日から遡って最後の該当曜日
        nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        d = nxt - timedelta(days=1)
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        return d

    return {
        observed(date(year, 1, 1)),        # 元日
        nth_weekday(1, 0, 3),              # MLKデー（1月第3月曜）
        nth_weekday(2, 0, 3),              # 大統領の日（2月第3月曜）
        last_weekday(5, 0),                # メモリアルデー（5月最終月曜）
        observed(date(year, 6, 19)),       # ジューンティーンス
        observed(date(year, 7, 4)),        # 独立記念日
        nth_weekday(9, 0, 1),              # レイバーデー（9月第1月曜）
        nth_weekday(11, 3, 4),             # 感謝祭（11月第4木曜）
        observed(date(year, 12, 25)),      # クリスマス
    }


def nth_business_day(year: int, month: int, n: int) -> date:
    """指定年月の第n営業日（土日・米主要祝日を除く）を返す。"""
    holidays = us_federal_holidays(year)
    d = date(year, month, 1)
    count = 0
    while True:
        if d.weekday() < 5 and d not in holidays:
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)


def make_ism_event(kind: str, tgt_year: int, tgt_month: int, pub_date: date) -> dict:
    """ISM景況指数（10:00 ET）の event dict を作る。kind は 'mfg' / 'svc'。"""
    offset = et_offset_str(pub_date, 10, 0)
    datetime_local = f"{pub_date.strftime('%Y-%m-%d')}T10:00:00{offset}"
    datetime_utc = to_utc_z(datetime_local)
    name = "ISM製造業景況指数" if kind == "mfg" else "ISM非製造業景況指数"
    if tgt_year != pub_date.year:
        title = f"{name} ({tgt_year}年{JP_MONTH_LABEL[tgt_month]}分)"
    else:
        title = f"{name} ({JP_MONTH_LABEL[tgt_month]}分)"
    desc = (
        "全米の製造業購買担当者への調査（PMI）。50が好況/不況の分かれ目。月初一番乗りの景況指標で相場が動きやすい。"
        if kind == "mfg"
        else "サービス業など非製造業の購買担当者調査（PMI）。50が好況/不況の分かれ目。米経済はサービス業が主体のため注目度が高い。"
    )
    return {
        "id": f"us_ism_{kind}_{tgt_year}-{tgt_month:02d}",
        "title": title,
        "category": "ISM",
        "country": "US",
        "datetime_utc": datetime_utc,
        "datetime_local": datetime_local,
        "timezone": "America/New_York",
        "importance": 2,
        "is_estimated": True,   # ルール計算の近似（公式カレンダー未取得）
        "description": desc,
        "source_url": ISM_SOURCE_URL,
        "result": None,
    }


def build_us_ism(target_years: set[int]) -> list[dict]:
    """対象年のISM製造業・非製造業をルール計算で生成する（公表年でループ）。"""
    events: list[dict] = []
    for pub_year in sorted(target_years):
        for pub_month in range(1, 13):
            # 対象月 = 公表月の前月（1月公表 → 前年12月分）
            if pub_month == 1:
                tgt_year, tgt_month = pub_year - 1, 12
            else:
                tgt_year, tgt_month = pub_year, pub_month - 1
            # 製造業: 第1営業日（1月のみ第2営業日）
            mfg_n = 2 if pub_month == 1 else 1
            events.append(make_ism_event("mfg", tgt_year, tgt_month,
                                         nth_business_day(pub_year, pub_month, mfg_n)))
            # 非製造業: 第3営業日
            events.append(make_ism_event("svc", tgt_year, tgt_month,
                                         nth_business_day(pub_year, pub_month, 3)))
    log(f"  fetch_schedules[ISM]: ルール計算で {len(events)} 件生成")
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
        record_fetch_warning("fetch_schedules[PCE]", f"想定外の失敗 ({type(e).__name__}) → イベント生成なし")
    # 2. 米CPI（独立try/except）
    try:
        all_events.extend(fetch_bls_cpi())
    except Exception as e:
        log(f"  fetch_schedules[CPI]: 想定外の失敗 ({type(e).__name__}: {e}) → スキップ")
        record_fetch_warning("fetch_schedules[CPI]", f"想定外の失敗 ({type(e).__name__}) → イベント生成なし")
    # 3. 全国CPI（独立try/except、ルール計算）
    try:
        all_events.extend(build_jp_cpi(target_years))
    except Exception as e:
        log(f"  fetch_schedules[JP CPI]: 想定外の失敗 ({type(e).__name__}: {e}) → スキップ")
        record_fetch_warning("fetch_schedules[JP CPI]", f"想定外の失敗 ({type(e).__name__}) → イベント生成なし")
    # 4. 米雇用統計（独立try/except、ルール計算＝第1金曜）
    try:
        all_events.extend(build_us_jobs(target_years))
    except Exception as e:
        log(f"  fetch_schedules[JOBS]: 想定外の失敗 ({type(e).__name__}: {e}) → スキップ")
        record_fetch_warning("fetch_schedules[JOBS]", f"想定外の失敗 ({type(e).__name__}) → イベント生成なし")
    # 5. 米PPI（独立try/except、BLS公式→真値表フォールバック）
    try:
        all_events.extend(fetch_bls_ppi())
    except Exception as e:
        log(f"  fetch_schedules[PPI]: 想定外の失敗 ({type(e).__name__}: {e}) → スキップ")
        record_fetch_warning("fetch_schedules[PPI]", f"想定外の失敗 ({type(e).__name__}) → イベント生成なし")
    # 6. 米GDP（独立try/except、BEA公式→真値表フォールバック）
    try:
        all_events.extend(fetch_bea_gdp())
    except Exception as e:
        log(f"  fetch_schedules[GDP]: 想定外の失敗 ({type(e).__name__}: {e}) → スキップ")
        record_fetch_warning("fetch_schedules[GDP]", f"想定外の失敗 ({type(e).__name__}) → イベント生成なし")
    # 7. 米小売売上高（独立try/except、Census公式→真値表フォールバック）
    try:
        all_events.extend(fetch_census_retail())
    except Exception as e:
        log(f"  fetch_schedules[RETAIL]: 想定外の失敗 ({type(e).__name__}: {e}) → スキップ")
        record_fetch_warning("fetch_schedules[RETAIL]", f"想定外の失敗 ({type(e).__name__}) → イベント生成なし")
    # 8. ISM景況指数（独立try/except、ルール計算）
    try:
        all_events.extend(build_us_ism(target_years))
    except Exception as e:
        log(f"  fetch_schedules[ISM]: 想定外の失敗 ({type(e).__name__}: {e}) → スキップ")
        record_fetch_warning("fetch_schedules[ISM]", f"想定外の失敗 ({type(e).__name__}) → イベント生成なし")

    # target_years でフィルタ（datetime_local の年＝公表年で判定）。
    # ただし年跨ぎイベント（2026年12月分の雇用統計＝2027-01-08公表）は公表年が
    # target_years(=[2026]) の外に出るため、そのままでは弾かれてシードの誤日付が
    # 生き残る。既にシードに id がある行＝維持すべき既存ページなので必ず残す。
    alias_ids = set(_SEED_ID_ALIAS.values())
    filtered = [
        e for e in all_events
        if event_year(e) in target_years or e["id"] in alias_ids
    ]
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
