# -*- coding: utf-8 -*-
"""決算「日」の解決まわりのテスト。

守りたいこと:
  1. 過去の発表実績から次回発表日を予測できる（公式カレンダーの圏外を埋める）
  2. 決算以外の開示が履歴に混ざっても、3ヶ月手前の日付に引っ張られない
  3. yfinance が返す過去日（決算通過直後の既知の癖）を採用しない
  4. その決算日がどのソース由来かをイベントに残す
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_earnings as fe  # noqa: E402

# AAPL の実際の 8-K(Item 2.02) 提出日（新しい順）
AAPL = ["2026-07-30", "2026-04-30", "2026-01-29", "2025-10-30",
        "2025-07-31", "2025-05-01", "2025-01-30", "2024-10-31"]


def test_predicts_next_quarter_from_history():
    """昨年同四半期(2025-10-30)の364日後＝2026-10-29 を次回として返す。"""
    got = fe.next_from_history(AAPL, date(2026, 8, 27))
    assert got == date(2026, 10, 29)


def test_history_noise_does_not_pull_prediction_forward():
    """決算以外の 8-K(2025-09-05) が混ざっても anchored なら影響を受けない。"""
    noisy = ["2026-07-30", "2026-04-30", "2026-01-29", "2025-10-30",
             "2025-09-05", "2025-07-31", "2025-05-01", "2025-01-30"]
    today = date(2026, 8, 27)
    assert fe.next_from_history(noisy, today, rule="nearest") == date(2026, 9, 4)
    assert fe.next_from_history(noisy, today) == date(2026, 10, 29)


def test_no_prediction_without_a_year_of_history():
    """履歴が1年ぶん(4本)に満たなければ当てずっぽうを出さない。"""
    assert fe.next_from_history(AAPL[:3], date(2026, 8, 27)) is None


def test_prediction_is_never_in_the_past():
    """今日以降の候補が無ければ None（古い履歴で過去日を返さない）。"""
    old = ["2020-01-28", "2019-10-30", "2019-07-30", "2019-04-30"]
    assert fe.next_from_history(old, date(2026, 8, 27)) is None


def test_future_only_rejects_stale_date():
    """yfinance が前回の決算日を返し続ける癖（YUM 7/30 事象）を弾く。"""
    today = date(2026, 8, 27)
    assert fe._future_only(date(2026, 7, 30), today, "yfinance", "YUM") is None
    assert fe._future_only(today, today, "yfinance", "YUM") == today
    assert fe._future_only(date(2026, 9, 1), today, "yfinance", "YUM") == date(2026, 9, 1)


def _hold(**kw):
    base = {"name": "テスト", "ticker": "AAPL", "market": "米国",
            "page_id": None, "current": None, "date_prop": "次回決算日",
            "src": "Nasdaq", "session": "PM", "time_hhmm": "16:30", "cross_gap": None}
    base.update(kw)
    return base


def test_event_records_date_source():
    ev = fe.build_event(_hold(), date(2026, 10, 29))
    assert ev["date_source"] == "Nasdaq"
    assert "Nasdaq公式" in ev["description"]


def test_jpx_source_is_treated_as_confirmed():
    ev = fe.build_event(_hold(ticker="7974", market="日本", src="JPX"), date(2026, 11, 6))
    assert ev["is_estimated"] is False
    assert "JPX" in ev["description"]


def test_estimated_sources_stay_estimated_and_warn_on_gap():
    ev = fe.build_event(_hold(src="yfinance", cross_gap=6), date(2026, 10, 29))
    assert ev["is_estimated"] is True
    assert ev["date_source"] == "yfinance"
    assert "⚠️" in ev["description"] and "6日" in ev["description"]


def test_future_only_rejects_weekend():
    """土日の決算日は存在しない（取引所が閉まっている）ので採用しない。"""
    today = date(2026, 8, 27)
    assert fe._future_only(date(2026, 9, 5), today, "yfinance", "X") is None   # 土
    assert fe._future_only(date(2026, 9, 6), today, "yfinance", "X") is None   # 日
    assert fe._future_only(date(2026, 9, 7), today, "yfinance", "X") == date(2026, 9, 7)


def test_snap_moves_japanese_holiday_to_next_open_day():
    """予測は曜日を保つので祝日に当たることがある（任天堂 2026-11-03＝文化の日）。"""
    assert fe.snap_to_open_day(date(2026, 11, 3), "日本", "任天堂") == date(2026, 11, 4)


def test_snap_moves_weekend_to_monday():
    assert fe.snap_to_open_day(date(2026, 9, 5), "日本", "X") == date(2026, 9, 7)


def test_snap_keeps_normal_business_day():
    assert fe.snap_to_open_day(date(2026, 11, 5), "日本", "X") == date(2026, 11, 5)


def test_snap_moves_us_holiday():
    """米国も同様（2026-11-26 は感謝祭で休場）。"""
    assert fe.snap_to_open_day(date(2026, 11, 26), "米国", "X") == date(2026, 11, 27)
