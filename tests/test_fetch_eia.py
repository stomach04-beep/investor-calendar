"""
fetch_eia.py の回帰テスト（ネットワーク不要）。

守りたい性質:
  1. id は「発表日」でなく「対象週の金曜」で固定＝祝日で水→木にズレても不変
     （[[feedback_upsert_id_immutable]]。ID が動くと Notion に重複行ができる）
  2. 通常水曜は is_estimated=True、公式表の祝日繰り下げは False
     （通常水曜を確定にすると、後から繰り下げが公表されたとき保護されて凍結する）
  3. 米国夏時間/冬時間で ET オフセットが正しく切り替わる
  4. 真値表が EIA 公式ページの転記と一致している（切れ／誤記の検出）

実行:
    pip install -r requirements-dev.txt
    python -m pytest tests/test_fetch_eia.py -v
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_eia as fe  # noqa: E402


# ----------------------------------------------------------------------
# 公式ページのHTML断片（2026-07-26 時点の実物から抜粋）
# ----------------------------------------------------------------------
SAMPLE_HTML = """
<table>
  <tr><th>Data for the week ending</th><th>Alternate release date</th>
      <th>Release day</th><th>Release time</th><th>Holiday</th></tr>
  <tr><td>December 19, 2025</td><td>December 29, 2025</td>
      <td>Monday</td><td>5:00 p.m.</td><td>Christmas</td></tr>
  <tr><td>January 16, 2026</td><td>January 22, 2026</td>
      <td>Thursday</td><td>12:00 p.m.</td><td>Martin Luther King Jr. Day</td></tr>
  <tr><td>December 27, 2024</td><td>January 2, 2025</td>
      <td>Thursday</td><td>11:00 a.m.</td><td>New Year's Day</td></tr>
</table>
"""


# ----------------------------------------------------------------------
# 時刻・日付パース
# ----------------------------------------------------------------------
def test_parse_us_time_am_pm_and_noon():
    assert fe._parse_us_time("10:30 a.m.") == (10, 30)
    assert fe._parse_us_time("11:00 a.m.") == (11, 0)
    assert fe._parse_us_time("12:00 p.m.") == (12, 0)   # 正午は12時のまま
    assert fe._parse_us_time("5:00 p.m.") == (17, 0)
    assert fe._parse_us_time("12:00 a.m.") == (0, 0)    # 深夜0時
    assert fe._parse_us_time("なにか") is None


def test_parse_us_date():
    assert fe._parse_us_date("January 22, 2026") == date(2026, 1, 22)
    assert fe._parse_us_date("ヘッダ行") is None


# ----------------------------------------------------------------------
# 公式スケジュール表のパース
# ----------------------------------------------------------------------
def test_parse_schedule_html_keys_on_normal_wednesday():
    """表のキーは『本来の公表水曜』＝対象週の金曜+5日になっていること。"""
    parsed = fe.parse_schedule_html(SAMPLE_HTML)
    # 1/16(金) の週 → 本来 1/21(水) → 実際は 1/22(木) 12:00
    assert parsed[date(2026, 1, 21)] == (date(2026, 1, 22), 12, 0, "Martin Luther King Jr. Day")
    # クリスマス週は木曜ですらなく月曜17:00（ルールで推測できない実例）
    assert parsed[date(2025, 12, 24)] == (date(2025, 12, 29), 17, 0, "Christmas")
    # ヘッダ行は取り込まれない
    assert len(parsed) == 3


def test_parse_schedule_html_ignores_garbage():
    assert fe.parse_schedule_html("<table><tr><td>foo</td></tr></table>") == {}


# ----------------------------------------------------------------------
# ローリング窓
# ----------------------------------------------------------------------
def test_wednesdays_in_window_all_wednesdays_and_bounded():
    today = date(2026, 7, 26)  # 日曜
    ws = fe.wednesdays_in_window(today, 14, 120)
    assert all(w.weekday() == 2 for w in ws)             # 全部水曜
    assert ws[0] >= today - fe.timedelta(days=14)
    assert ws[-1] <= today + fe.timedelta(days=120)
    assert ws == sorted(ws)                              # 昇順
    # 週次なので窓幅134日 → 19〜20件
    assert 19 <= len(ws) <= 20


# ----------------------------------------------------------------------
# イベント生成
# ----------------------------------------------------------------------
def test_normal_week_is_wednesday_1030_et_and_estimated():
    ev = fe.build_eia_event(date(2026, 7, 22), {})
    assert ev["id"] == "us_eia_2026-07-17"               # 対象週の金曜
    assert ev["datetime_local"] == "2026-07-22T10:30:00-04:00"   # 夏時間
    assert ev["is_estimated"] is True
    assert ev["category"] == "EIA"
    assert ev["country"] == "US"
    assert ev["importance"] == 1


def test_holiday_week_uses_official_date_and_is_confirmed():
    overrides = {date(2026, 9, 9): (date(2026, 9, 10), 12, 0, "Labor Day")}
    ev = fe.build_eia_event(date(2026, 9, 9), overrides)
    assert ev["datetime_local"] == "2026-09-10T12:00:00-04:00"
    assert ev["is_estimated"] is False                   # 公式表＝確定
    assert "Labor Day" in ev["description"]


def test_id_is_invariant_across_holiday_shift():
    """同じ対象週なら、繰り下がっても id が変わらない（Notion重複の防止）。"""
    wednesday = date(2026, 9, 9)
    plain = fe.build_eia_event(wednesday, {})
    shifted = fe.build_eia_event(
        wednesday, {wednesday: (date(2026, 9, 10), 12, 0, "Labor Day")}
    )
    assert plain["id"] == shifted["id"] == "us_eia_2026-09-04"
    assert plain["datetime_local"] != shifted["datetime_local"]


def test_winter_time_offset_switches():
    """米国夏時間は11月第1日曜で終わる。11/4は EST(-05:00)。"""
    summer = fe.build_eia_event(date(2026, 10, 28), {})
    winter = fe.build_eia_event(date(2026, 11, 4), {})
    assert summer["datetime_local"].endswith("-04:00")
    assert winter["datetime_local"].endswith("-05:00")


def test_utc_and_local_are_consistent():
    ev = fe.build_eia_event(date(2026, 7, 22), {})
    assert ev["datetime_utc"] == "2026-07-22T14:30:00Z"  # 10:30 EDT = 14:30 UTC


# ----------------------------------------------------------------------
# 真値表（フォールバック）の健全性
# ----------------------------------------------------------------------
def test_truth_table_keys_are_wednesdays_and_shift_forward():
    for normal_wed, (actual, hour, _minute, holiday) in fe.EIA_HOLIDAY_TRUTH.items():
        assert normal_wed.weekday() == 2, f"{normal_wed} が水曜でない"
        assert actual > normal_wed, f"{normal_wed} の繰り下げ先が過去"
        assert 0 <= hour <= 23
        assert holiday, "祝日名が空"


def test_offline_mode_uses_truth_table_without_network():
    overrides, official_ok = fe.fetch_holiday_overrides(offline=True)
    assert official_ok is False
    assert overrides == fe.EIA_HOLIDAY_TRUTH


def test_build_all_offline_marks_only_truth_table_weeks_confirmed():
    events = fe.build_all(date(2026, 8, 30), fe.EIA_HOLIDAY_TRUTH, official_ok=False)
    confirmed = [e for e in events if not e["is_estimated"]]
    # 2026-08-30 起点の窓（-14〜+120日 ≒ 8/16〜12/28）には
    # Labor Day(9/10)・Columbus Day(10/15)・Veterans Day(11/12) が入る
    assert {e["id"] for e in confirmed} == {
        "us_eia_2026-09-04", "us_eia_2026-10-09", "us_eia_2026-11-06",
    }
