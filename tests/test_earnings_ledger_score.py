# -*- coding: utf-8 -*-
"""日付履歴の採点（earnings_ledger_score.py）のテスト（ネットワーク不要）。

守りたいこと:
  1. 履歴の文字列を壊れた要素があっても読める
  2. 発表日より前の記録だけを採点し、同じ出どころ×リード区分は1件に数える
  3. 複数四半期ぶんの履歴を、それぞれの実発表日に向けて分けて採点する
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import earnings_ledger_score as els  # noqa: E402


def test_parse_log_skips_broken_items():
    got = els.parse_log("2026-09-23:2026-10-29|Nasdaq見込み / こわれた / 2026-10-01:2026-10-29|Nasdaq")
    assert got == [(date(2026, 9, 23), date(2026, 10, 29), "Nasdaq見込み"),
                   (date(2026, 10, 1), date(2026, 10, 29), "Nasdaq")]


def test_score_only_before_actual_and_dedups_by_bucket():
    entries = [
        (date(2026, 9, 1), date(2026, 10, 28), "yfinance"),     # 57日前 → 31日以上前・-1日
        (date(2026, 9, 23), date(2026, 10, 29), "Nasdaq見込み"),  # 36日前 → 31日以上前・ピタリ
        (date(2026, 10, 1), date(2026, 10, 29), "Nasdaq見込み"),  # 28日前 → 8-30日前・ピタリ
        (date(2026, 10, 20), date(2026, 10, 29), "Nasdaq"),      # 9日前 → 8-30日前
        (date(2026, 10, 29), date(2027, 1, 28), "EDGAR予測"),     # 当日の記録は次の四半期＝採点しない
    ]
    rows = els.score(entries, date(2026, 10, 29))
    assert ("yfinance", "31日以上前", -1) in rows
    assert ("Nasdaq見込み", "31日以上前", 0) in rows
    assert ("Nasdaq見込み", "8-30日前", 0) in rows
    assert ("Nasdaq", "8-30日前", 0) in rows
    assert not any(src == "EDGAR予測" for src, _, _ in rows)


def test_evaluate_splits_history_per_actual_date():
    rows = [{"id": "hold_earnings_us_TST",
             "log": "2026-07-01:2026-07-30|yfinance / 2026-08-05:2026-10-29|EDGAR予測 / 2026-10-20:2026-10-29|Nasdaq",
             "current": None}]
    actual_us = {"TST": [date(2026, 10, 29), date(2026, 7, 30)]}
    scored = els.evaluate(rows, actual_us, {}, today=date(2026, 11, 1))
    # 7/30 に向けた記録は 7/1 の1件、10/29 に向けた記録は 8/5 と 10/20 の2件
    assert ("yfinance", "8-30日前", 0) in scored
    assert ("EDGAR予測", "31日以上前", 0) in scored
    assert ("Nasdaq", "8-30日前", 0) in scored
    assert len(scored) == 3


def test_evaluate_skips_without_actual():
    rows = [{"id": "hold_earnings_jp_9999", "log": "2026-09-23:2026-11-05|yfinance", "current": None}]
    assert els.evaluate(rows, {}, {}, today=date(2026, 12, 1)) == []


def test_summarize_counts_buckets():
    agg = els.summarize([("yfinance", "8-30日前", 0), ("yfinance", "8-30日前", 2), ("yfinance", "8-30日前", -5)])
    a = agg[("yfinance", "8-30日前")]
    assert (a["n"], a["exact"], a["pm1"], a["pm3"]) == (3, 1, 1, 2)
