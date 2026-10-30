"""
日本の公式日程（fetch_jp_schedules）・規則表（release_rules）・検査（check_release_rules）の回帰テスト。
ネットワーク不要（公式の実物を tests/fixtures に保存してある・2026-10-30 取得）。

守りたい性質:
  1. 内閣府・日銀・総務省の実物から日程を読める（解析部が本物で一度も動いていない事故の再発防止）
  2. 日本GDP・短観は 8:50 JST（UTC は前日 23:50Z）。「前日 23:50 JST」の二重がけを作らない
  3. id はシードのものを引き継ぐ（日付入り id でも変えない＝Notion の重複を作らない）
  4. 権利付き最終日・大納会・大発会が休場日を正しく避ける
  5. 公式から読めた値（official_live）だけが確定行を上書きできる
  6. 検査が「時刻のずれ」と「休場日」を拾い、正しい予定は拾わない

実行:
    python -m pytest tests/test_jp_schedules.py -v
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_release_rules as crr  # noqa: E402
import fetch_jp_schedules as fj  # noqa: E402
import notion_upsert as nu  # noqa: E402
from release_rules import at_rule_time, rule_by_prefix, rule_for  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
ESRI = (FIX / "esri_kouhyou_2026-10-30.html").read_bytes()
BOJ = (FIX / "boj_tkohyos_2026-10-30.xlsx").read_bytes()
STAT = (FIX / "stat_cpi_2026-10-30.xml").read_bytes()

# 2026-10-30 時点のシードにあった誤った行（前日 23:50 JST・題名の調査月違い）
SEED = [
    {"id": "jp_gdp_2026-11-17", "title": "7-9月期GDP速報値", "is_estimated": True,
     "datetime_utc": "2026-11-16T14:50:00Z", "importance": 2},
    {"id": "jp_gdp_2026-02-16", "title": "2025年10-12月期GDP速報値", "is_estimated": True,
     "datetime_utc": "2026-02-15T14:50:00Z", "importance": 2},
    {"id": "jp_tankan_2026-12-11", "title": "日銀短観 (12月調査)", "is_estimated": True,
     "datetime_utc": "2026-12-10T14:50:00Z", "importance": 3},
    {"id": "jp_tankan_2026-04-01", "title": "日銀短観 (4月調査)", "is_estimated": True,
     "datetime_utc": "2026-03-31T14:50:00Z", "importance": 3},
    {"id": "jp_exdiv_2026-12-29", "title": "12月度 権利付き最終日", "category": "DIVIDEND",
     "is_estimated": True, "datetime_utc": "2026-12-28T21:00:00Z"},
    {"id": "jp_market_2026-12-31", "title": "大納会", "category": "MARKET",
     "is_estimated": True, "datetime_utc": "2026-12-30T15:00:00Z"},
]


def _by_id(events):
    return {e["id"]: e for e in events}


# ---------------------------------------------------------------- 日本GDP
def test_esri_rows_from_real_page():
    rows = fj.parse_esri_schedule(ESRI)
    assert rows[0] == (2026, 7, "1st", date(2026, 11, 16), 8, 50)
    assert (2026, 7, "2nd", date(2026, 12, 8), 8, 50) in rows
    assert len(rows) == 4   # 四半期別GDP速報の表だけ（年次推計・資本ストックは読まない）


def test_gdp_keeps_seed_id_and_uses_850_jst():
    ev = _by_id(fj.build_jp_gdp(SEED, ESRI))["jp_gdp_2026-11-17"]
    assert ev["datetime_local"] == "2026-11-16T08:50:00+09:00"
    assert ev["datetime_utc"] == "2026-11-15T23:50:00Z"
    assert ev["is_estimated"] is False and ev["official_live"] is True


def test_gdp_seed_only_row_gets_rule_time_but_stays_estimated():
    ev = _by_id(fj.build_jp_gdp(SEED, ESRI))["jp_gdp_2026-02-16"]
    assert ev["datetime_local"] == "2026-02-16T08:50:00+09:00"
    assert ev["is_estimated"] is True and "official_live" not in ev


def test_confirmed_seed_row_is_not_repaired_back_to_id_date():
    confirmed = dict(SEED[0], is_estimated=False, datetime_utc="2026-11-15T23:50:00Z")
    events = fj.build_jp_gdp([confirmed], None)   # 公式が読めない日
    assert events == []   # id の日付（11/17）へ戻さない


# ---------------------------------------------------------------- 日銀短観
def test_boj_tankan_from_real_xlsx():
    rows = fj.parse_boj_tankan(BOJ)
    assert (2026, 12, date(2026, 12, 14), 8, 50) in rows
    assert (2027, 3, date(2027, 4, 1), 8, 50) in rows


def test_tankan_old_id_maps_to_survey_month_and_title_fixed():
    events = _by_id(fj.build_jp_tankan(SEED, BOJ))
    dec = events["jp_tankan_2026-12-11"]
    assert dec["datetime_local"] == "2026-12-14T08:50:00+09:00"
    assert dec["title"] == "日銀短観 (12月調査)"
    assert events["jp_tankan_2026-04-01"]["title"] == "日銀短観 (3月調査)"   # 旧題「4月調査」を直す


# ---------------------------------------------------------------- 全国CPI
def test_stat_cpi_reads_monthly_only():
    rows = fj.parse_stat_cpi(STAT)
    assert (2026, 2, date(2026, 3, 24), 8, 30) in rows   # ルール計算は 3/20 で外れていた
    assert all(1 <= m <= 12 for _, m, _, _, _ in rows)   # 「2025年平均」などは読まない


def test_rule_cpi_is_estimated():
    import fetch_schedules as fs
    evs = fs.build_jp_cpi({2026})
    assert evs and all(e["is_estimated"] is True for e in evs)
    assert all(e["datetime_local"].endswith("T08:30:00+09:00") for e in evs)


# ---------------------------------------------------------------- 市場日程
def test_exdiv_and_year_end_dates():
    assert fj.exdiv_last_day(2026, 12) == date(2026, 12, 28)   # 12/30 大納会の2営業日前
    assert fj.exdiv_last_day(2026, 3) == date(2026, 3, 27)
    assert fj.exdiv_last_day(2027, 3) == date(2027, 3, 29)
    assert fj.last_open_day(2026) == date(2026, 12, 30)
    assert fj.first_open_day(2027) == date(2027, 1, 4)


def test_market_calendar_keeps_seed_ids():
    events = _by_id(fj.build_jp_market_calendar(SEED, [2026]))
    assert events["jp_exdiv_2026-12-29"]["datetime_local"] == "2026-12-28T15:30:00+09:00"
    assert events["jp_market_2026-12-31"]["datetime_local"] == "2026-12-30T15:30:00+09:00"


# ---------------------------------------------------------------- upsert の保護
def test_only_official_live_overrides_confirmed_rows():
    page = {"properties": {}}
    names = {"datetime": "発表日時"}
    assert nu.allow_confirmed_update({"id": "jp_cpi_x", "official_live": True}, page, names)
    assert not nu.allow_confirmed_update({"id": "jp_cpi_x", "is_estimated": False}, page, names)


# ---------------------------------------------------------------- 検査
def test_checker_flags_double_shift_and_holiday():
    today = date(2026, 10, 30)
    problems = crr.find_problems(SEED, today)
    text = "\n".join(problems)
    assert "jp_gdp_2026-11-17" in text and "23:50" in text
    assert "jp_market_2026-12-31" in text and "休場日" in text


def test_checker_passes_correct_events():
    today = date(2026, 10, 30)
    good = fj.build_jp_gdp(SEED, ESRI) + fj.build_jp_tankan(SEED, BOJ) \
        + fj.build_jp_market_calendar(SEED, [2026, 2027])
    assert crr.find_problems(good, today) == []


def test_rule_table_us_dst():
    # 規則は現地時刻で持つので、夏冬時間の切替で UTC が自動で1時間動く
    r = rule_by_prefix("us_cpi_")
    assert at_rule_time(r, date(2026, 10, 14))[1] == "2026-10-14T12:30:00Z"
    assert at_rule_time(r, date(2026, 11, 10))[1] == "2026-11-10T13:30:00Z"
    assert rule_for({"id": "us_fomc_minutes_2026-11-18"}).key == "us_fomc_minutes"
