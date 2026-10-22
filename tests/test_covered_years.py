"""
対象年（covered_years）の回帰テスト（ネットワーク不要）。

2029-04 の単発の行だけで covered_years に 2029 が入り、行の無い 2028 が抜けたため、
雇用統計・ISM などの定期イベントが 2027 の次に 2029 へ飛んでいた（2026-10-23 修正）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_events as be  # noqa: E402
import common  # noqa: E402


def test_fill_year_gaps_fills_missing_year():
    assert common.fill_year_gaps([2026, 2027, 2029]) == [2026, 2027, 2028, 2029]
    assert common.fill_year_gaps({2029, 2026}) == [2026, 2027, 2028, 2029]


def test_fill_year_gaps_edge_cases():
    assert common.fill_year_gaps([]) == []
    assert common.fill_year_gaps(["2027"]) == [2027]


def test_build_events_covers_gap_year(monkeypatch):
    """シードの covered_years に穴があっても、build_events の出力では埋まっていること。"""
    seed = {
        "covered_years": [2026, 2027, 2029],
        "events": [{"id": "us_x_2026-01-01", "title": "x", "category": "OTHER", "country": "US",
                    "datetime_utc": "2026-01-01T00:00:00Z",
                    "datetime_local": "2026-01-01T00:00:00+00:00"}],
    }
    monkeypatch.setattr(be, "load_canonical_events", lambda: seed)
    out = be.build([])
    assert out["covered_years"] == [2026, 2027, 2028, 2029]
