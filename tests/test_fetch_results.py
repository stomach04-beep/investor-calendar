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


# ----------------------------------------------------------------------
# 雇用統計・CPI・PCE: id は公表日ベースなので、シードの題名「(◯月分)」から作る対応表で対象月を引く
#   2025-10 の政府閉鎖で雇用統計 9月分は 2025-11-20 公表、10月分と11月分は 12-16 にまとめて公表
#   （ALFRED の時点別データで確認）。発表日の前月で決めると別の月になる。
# ----------------------------------------------------------------------
import fetch_schedules as fs  # noqa: E402

SEED = {"events": [
    {"id": "us_nfp_2025-10-03", "category": "JOBS", "country": "US", "title": "米雇用統計 (9月分)"},
    {"id": "us_nfp_2025-11-07", "category": "JOBS", "country": "US", "title": "米雇用統計 (10月分)"},
    {"id": "us_pce_2026-01-26", "category": "PCE", "country": "US", "title": "米PCEデフレーター (2025年12月分)"},
    {"id": "us_retail_2026-03", "category": "RETAIL", "country": "US", "title": "米小売売上高 (3月分)"},
]}

# 雇用者数（千人）と失業率はテスト用に作った値（実データではない）。10月の失業率は欠測の形を再現
PAYEMS = [(date(2025, 8, 1), 159000.0), (date(2025, 9, 1), 159076.0),
          (date(2025, 10, 1), 158936.0), (date(2025, 11, 1), 158977.0)]
UNRATE = [(date(2025, 9, 1), 4.4), (date(2025, 11, 1), 4.5)]


def test_target_month_by_id_reads_seed_titles(monkeypatch):
    monkeypatch.setattr(fs, "load_canonical_events", lambda: SEED)
    m = fr.load_target_month_by_id()
    assert m["us_nfp_2025-10-03"] == (2025, 9)
    assert m["us_nfp_2025-11-07"] == (2025, 10)
    assert m["us_pce_2026-01-26"] == (2025, 12)
    assert "us_retail_2026-03" not in m   # 小売は id の正規表現側で扱う


def test_target_month_by_id_survives_unreadable_seed(monkeypatch):
    def boom():
        raise OSError("seed missing")
    monkeypatch.setattr(fs, "load_canonical_events", boom)
    assert fr.load_target_month_by_id() == {}


def test_jobs_uses_row_target_month_for_delayed_release():
    """雇用統計 9月分（2025-11-20 公表）に、発表日の前月（10月）でなく9月の値を書くこと。"""
    cache = {"PAYEMS": list(PAYEMS), "UNRATE": list(UNRATE)}
    res = fr.build_result("JOBS", date(2025, 11, 20), cache, (2025, 9))
    assert res == "非農業部門雇用者数 前月比+7.6万人 / 失業率 4.4%（2025年9月分）"


def test_jobs_two_months_released_same_day_stay_separate():
    """10月分と11月分は同じ 12-16 公表。それぞれ自分の月の値になること（10月は失業率なし）。"""
    cache = {"PAYEMS": list(PAYEMS), "UNRATE": list(UNRATE)}
    oct_ = fr.build_result("JOBS", date(2025, 12, 16), cache, (2025, 10))
    nov = fr.build_result("JOBS", date(2025, 12, 16), cache, (2025, 11))
    assert oct_ == "非農業部門雇用者数 前月比-14.0万人（2025年10月分）"
    assert nov == "非農業部門雇用者数 前月比+4.1万人 / 失業率 4.5%（2025年11月分）"
