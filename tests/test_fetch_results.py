"""
fetch_results.py の回帰テスト（ネットワーク不要）。

小売売上高の結果は「行の対象月」で計算する（2026-10-23）。
発表日の前月で決めると、2025-10 の米政府閉鎖で1〜2か月遅れて公表された月
（例: 2026年3月分 = 2026-04-21 公表）に、別の月の前月比を書いてしまっていた。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_results as fr  # noqa: E402

# FRED RSAFS の実値（2026-10-23 取得分の一部・百万ドル）。cache に入れて load_fred を呼ばせない
RSAFS = [
    (date(2026, 1, 1), 701860.0),
    (date(2026, 2, 1), 707959.0),
    (date(2026, 3, 1), 719700.0),
    (date(2026, 4, 1), 723921.0),
]


def _cache() -> dict:
    return {"RSAFS": list(RSAFS)}


def test_target_ym_from_retail_id():
    assert fr.target_ym_from_id("us_retail_2026-03") == (2026, 3)
    assert fr.target_ym_from_id("us_retail_2025-12") == (2025, 12)
    # 対象月を持たない id は None（発表日から推測する従来の動きに任せる）
    assert fr.target_ym_from_id("us_cpi_2026-05-12") is None
    assert fr.target_ym_from_id("") is None


def test_retail_uses_row_target_month_for_delayed_release():
    """2026年3月分は 2026-04-21 公表。発表日の前月（4月）でなく3月の前月比を書くこと。"""
    res = fr.build_result("RETAIL", date(2026, 4, 21), _cache(), (2026, 3))
    assert res == "小売売上高 前月比+1.7%（2026年3月分）"


def test_retail_two_releases_in_one_month_get_different_months():
    """2026年4月は 1日（2月分）と 21日（3月分）の2回。同じ月の値が2行に入らないこと。"""
    feb = fr.build_result("RETAIL", date(2026, 4, 1), _cache(), (2026, 2))
    mar = fr.build_result("RETAIL", date(2026, 4, 21), _cache(), (2026, 3))
    assert "（2026年2月分）" in feb and "（2026年3月分）" in mar


def test_retail_without_target_month_falls_back_to_previous_month():
    """対象月が分からないときは、従来どおり発表日の前月で計算すること。"""
    res = fr.build_result("RETAIL", date(2026, 5, 14), _cache())
    assert res == "小売売上高 前月比+0.6%（2026年4月分）"
