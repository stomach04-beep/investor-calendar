# -*- coding: utf-8 -*-
"""決算の日付・時間帯の仕様見直し（2026-09-23）の回帰テスト（ネットワーク不要）。

守りたいこと:
  1. Nasdaq カレンダーは「時間帯が付いている行」だけ確定扱い。時間帯なし行は「見込み」
     （2,889行中2,058行が昨年+364日ちょうど＝機械予測だった）
  2. 曜日のクセ（直近8回すべて同じ曜日）に合わせて未確定の日付を寄せる
     （米国株92%・日本株82%。NVDA は水曜8/8なのに Yahoo が火曜を返した）
  3. 米国株の時刻は「Yahoo の時刻」と「8-K受理の最小値」の早い方（発表の後に通知が鳴らない）
  4. 件名と説明欄に時間帯（寄り前/引け後/場中）と日本時間換算が出る
  5. 日付履歴（Notion）は日付か出どころが変わったときだけ追記される
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_earnings as fe  # noqa: E402
import notion_upsert as nu  # noqa: E402
import us_earnings_time as ue  # noqa: E402


# ----------------------------------------------------------------------
# 1. 確定の判定
# ----------------------------------------------------------------------
def _us(**kw):
    base = {"name": "テスト", "ticker": "TST", "market": "米国", "src": "Nasdaq", "session": "PM"}
    base.update(kw)
    return base


def test_nasdaq_with_session_is_confirmed():
    ev = fe.build_event(_us(src="Nasdaq", session="AM"), date(2026, 10, 29))
    assert ev["is_estimated"] is False
    assert "確定" in ev["description"]


def test_nasdaq_without_session_is_only_an_outlook():
    ev = fe.build_event(_us(src="Nasdaq見込み", session=None), date(2026, 10, 29))
    assert ev["is_estimated"] is True
    assert ev["date_source"] == "Nasdaq見込み"
    assert "見込み" in ev["description"]
    assert "変更される場合" in ev["description"]


# ----------------------------------------------------------------------
# 2. 曜日のクセ
# ----------------------------------------------------------------------
NVDA = ["2026-08-26", "2026-05-20", "2026-02-25", "2025-11-19",
        "2025-08-27", "2025-05-28", "2025-02-26", "2024-11-20"]  # 全部水曜


def test_weekday_habit_detects_all_same_weekday():
    assert fe.weekday_habit(NVDA) == 2  # 水曜


def test_weekday_habit_requires_all_eight():
    mixed = NVDA[:7] + ["2024-11-19"]  # 1つだけ火曜
    assert fe.weekday_habit(mixed) is None
    assert fe.weekday_habit(NVDA[:5]) is None  # 8回に満たない


def test_habit_moves_yahoo_tuesday_to_wednesday():
    """NVDA: Yahoo の 2026-11-17(火) → クセの水曜 11-18 へ。"""
    moved, note = fe.adjust_to_habit_weekday(date(2026, 11, 17), NVDA, "米国", "NVDA")
    assert moved == date(2026, 11, 18)
    assert note and "水曜" in note


def test_habit_leaves_date_already_on_habit_weekday():
    moved, note = fe.adjust_to_habit_weekday(date(2026, 11, 18), NVDA, "米国", "NVDA")
    assert moved == date(2026, 11, 18) and note is None


def test_habit_moves_within_three_days_only():
    """日曜(11/22)が採用日なら水曜は4日前(11/18)と3日後(11/25)。3日以内の 11/25 へ寄せる。"""
    moved, note = fe.adjust_to_habit_weekday(date(2026, 11, 22), NVDA, "米国", "NVDA")
    assert moved == date(2026, 11, 25) and note


def test_habit_skips_when_target_is_holiday(monkeypatch):
    """クセの曜日が休場日なら会社も別の日にするので触らない。"""
    monkeypatch.setattr(fe, "closed_days", lambda market, years: {date(2026, 11, 18)})
    moved, note = fe.adjust_to_habit_weekday(date(2026, 11, 17), NVDA, "米国", "NVDA")
    assert moved == date(2026, 11, 17) and note is None


def test_habit_note_lands_in_description():
    ev = fe.build_event(_us(src="yfinance", habit_note="曜日のクセに合わせて寄せた"),
                        date(2026, 11, 18))
    assert "曜日のクセ" in ev["description"]


# ----------------------------------------------------------------------
# 3. 時刻＝早い側の上限
# ----------------------------------------------------------------------
def test_summarize_times_reports_earliest():
    r = ue.summarize_times(["07:46", "07:35", "07:49", "06:35"])  # JNJ 型
    assert r["earliest"] == "06:35"
    assert r["time"] >= "07:35"  # 中央値は遅い側


def test_pick_us_time_takes_earlier_of_yahoo_and_edgar():
    erow = {"earliest": "07:46", "time": "07:49"}
    yinfo = {"times": {"AM": "06:00", "PM": None}}
    t, src, note = fe._pick_us_time(erow, yinfo, "AM")
    assert t == "06:00" and src == "Yahoo+EDGAR"
    assert "06:00" in note and "07:46" in note


def test_pick_us_time_edgar_min_when_it_is_earlier():
    erow = {"earliest": "07:59", "time": "07:59"}
    yinfo = {"times": {"AM": "08:00", "PM": None}}
    t, _, _ = fe._pick_us_time(erow, yinfo, "AM")
    assert t == "07:59"


def test_pick_us_time_ignores_yahoo_of_other_session():
    erow = {"earliest": "16:30", "time": "16:30"}
    yinfo = {"times": {"AM": "06:00", "PM": None}}
    t, src, _ = fe._pick_us_time(erow, yinfo, "PM")
    assert t == "16:30" and src == "EDGAR実績"


def test_pick_us_time_without_any_source_falls_back():
    t, src, note = fe._pick_us_time({}, {"times": {}}, None)
    assert t is None and src == "既定" and note is None


# ----------------------------------------------------------------------
# 4. 時間帯の呼び名と件名・説明欄
# ----------------------------------------------------------------------
def test_session_label_us():
    assert fe.session_label("米国", 7, 0) == "寄り前"
    assert fe.session_label("米国", 9, 24) == "寄り前"
    assert fe.session_label("米国", 10, 0) == "場中"
    assert fe.session_label("米国", 16, 0) == "引け後"


def test_session_label_jp_uses_1530_close():
    assert fe.session_label("日本", 15, 30) == "引け後"
    assert fe.session_label("日本", 15, 0) == "場中"   # 東証は 15:30 引け
    assert fe.session_label("日本", 12, 0) == "昼休み"
    assert fe.session_label("日本", 8, 0) == "寄り前"


def test_title_and_description_show_session_and_jst():
    ev = fe.build_event(_us(src="Nasdaq", session="PM", time_hhmm="16:30"), date(2026, 10, 29))
    assert ev["title"] == "テスト 決算（引け後）"
    assert "米国 10/29(木) 引け後 16:30 ET" in ev["description"]
    assert "日本時間 10/30(金) 05:30" in ev["description"]


def test_jp_description_has_no_jst_conversion():
    ev = fe.build_event({"name": "任天堂", "ticker": "7974", "market": "日本",
                         "src": "JPX", "time_hhmm": "15:30"}, date(2026, 11, 4))
    assert ev["title"] == "任天堂 決算（引け後）"
    assert "日本時間" not in ev["description"]
    assert "日本 11/4(水) 引け後 15:30" in ev["description"]


def test_time_note_lands_in_description():
    ev = fe.build_event(_us(src="yfinance", time_hhmm="06:00",
                            time_note="Yahoo 06:00／8-K受理 06:35〜07:46。発表はこの間"),
                        date(2026, 10, 14))
    assert "8-K受理 06:35" in ev["description"]


# ----------------------------------------------------------------------
# 5. 日付履歴
# ----------------------------------------------------------------------
def _ev(local="2026-10-29T16:30:00-04:00", src="Nasdaq見込み", ev_id="hold_earnings_us_TST"):
    return {"id": ev_id, "datetime_local": local, "date_source": src}


def test_date_log_first_entry():
    got = nu.append_date_log("", _ev(), today=date(2026, 9, 23))
    assert got == "2026-09-23:2026-10-29|Nasdaq見込み"


def test_date_log_skips_when_unchanged():
    old = "2026-09-23:2026-10-29|Nasdaq見込み"
    assert nu.append_date_log(old, _ev(), today=date(2026, 9, 24)) is None


def test_date_log_appends_when_source_changes():
    old = "2026-09-23:2026-10-29|Nasdaq見込み"
    got = nu.append_date_log(old, _ev(src="Nasdaq"), today=date(2026, 10, 1))
    assert got == old + " / 2026-10-01:2026-10-29|Nasdaq"


def test_date_log_ignores_non_earnings():
    assert nu.append_date_log("", _ev(ev_id="us_cpi_2026-10-14"), today=date(2026, 9, 23)) is None


def test_date_log_is_capped():
    old = " / ".join(f"2026-01-{i % 28 + 1:02d}:2026-03-{i % 28 + 1:02d}|yfinance" for i in range(40))
    got = nu.append_date_log(old, _ev(), today=date(2026, 9, 23))
    assert got.count(" / ") == nu.DATE_LOG_MAX_ENTRIES - 1
    assert got.endswith("2026-09-23:2026-10-29|Nasdaq見込み")
