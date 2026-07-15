"""
fetch_schedules.py の回帰テスト（ネットワーク不要）。

2026-07 に発生した「米CPI 5件中4件・米PPI 5件中4件が日付誤り」「米雇用統計12月分が
2026-12-31 に生成される」バグの再発防止。

BLS は Akamai の bot ブロックで恒常的に 403 のため、CPI/PPI は毎回フォールバック
（真値表）で動く。したがって真値表そのものが正しいかをテストで固定する。

実行:
    pip install -r requirements-dev.txt
    python -m pytest tests/ -v
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_schedules as fs  # noqa: E402


@pytest.fixture(autouse=True)
def seed_alias():
    """
    各テスト前にシードid エイリアスを実データから構築する。
    fetch_schedules は collect_schedule_events() 内でこれを設定するため、
    個別関数を直接呼ぶテストでは自前で入れる必要がある。
    """
    fs._SEED_ID_ALIAS = fs.load_seed_id_alias()
    yield
    fs._SEED_ID_ALIAS = {}


def by_id(events: list[dict]) -> dict[str, dict]:
    return {e["id"]: e for e in events}


def local_date(event: dict) -> date:
    """イベントの現地日付（datetime_local の日付部分）を返す。"""
    return date.fromisoformat(event["datetime_local"][:10])


# ----------------------------------------------------------------------
# 米CPI: BLS公式(cpi.htm)の2026年日程。2026-07-15 に裏取り。
#   (対象年, 対象月) → (公表日, 据え置くべきシードid)
#   id は「日付の写像ではなく安定キー」。8/11→8/12 に直っても id は据え置き
#   （変えると notion_upsert が既存ページと突合できず重複ページが出る）。
# ----------------------------------------------------------------------
US_CPI_OFFICIAL = {
    (2026, 7): (date(2026, 8, 12), "us_cpi_2026-08-11"),
    (2026, 8): (date(2026, 9, 11), "us_cpi_2026-09-11"),
    (2026, 9): (date(2026, 10, 14), "us_cpi_2026-10-13"),
    (2026, 10): (date(2026, 11, 10), "us_cpi_2026-11-12"),
    (2026, 11): (date(2026, 12, 10), "us_cpi_2026-12-11"),
}

US_PPI_OFFICIAL = {
    (2026, 7): (date(2026, 8, 13), "us_ppi_2026-08-12"),
    (2026, 8): (date(2026, 9, 10), "us_ppi_2026-09-14"),
    (2026, 9): (date(2026, 10, 15), "us_ppi_2026-10-14"),
    (2026, 10): (date(2026, 11, 13), "us_ppi_2026-11-13"),
    (2026, 11): (date(2026, 12, 15), "us_ppi_2026-12-14"),
}


@pytest.mark.parametrize("target,expected", sorted(US_CPI_OFFICIAL.items()))
def test_cpi_matches_official_schedule(target, expected):
    """米CPIが公式日程と一致し、確定(is_estimated=False)で、idが据え置かれること。"""
    exp_date, exp_id = expected
    ev = by_id(fs.cpi_fallback()).get(exp_id)
    assert ev is not None, f"{exp_id} が生成されていない（真値表が対象月をカバーしていない疑い）"
    assert local_date(ev) == exp_date
    assert ev["is_estimated"] is False
    assert ev["datetime_local"].endswith(("-04:00", "-05:00"))
    assert "T08:30:00" in ev["datetime_local"]


@pytest.mark.parametrize("target,expected", sorted(US_PPI_OFFICIAL.items()))
def test_ppi_matches_official_schedule(target, expected):
    """米PPIが公式日程と一致し、確定(is_estimated=False)で、idが据え置かれること。"""
    exp_date, exp_id = expected
    ev = by_id(fs.ppi_fallback()).get(exp_id)
    assert ev is not None, f"{exp_id} が生成されていない（真値表が対象月をカバーしていない疑い）"
    assert local_date(ev) == exp_date
    assert ev["is_estimated"] is False
    assert "T08:30:00" in ev["datetime_local"]


def test_ppi_can_precede_cpi():
    """
    2026年8月分は PPI(9/10) が CPI(9/11) より先に出る。
    旧実装の「CPI公表日の翌営業日」近似ではこの月を構造的に表現できず 9/14 になっていた。
    近似に戻していないことを固定する。
    """
    ppi = by_id(fs.ppi_fallback())["us_ppi_2026-09-14"]
    cpi = by_id(fs.cpi_fallback())["us_cpi_2026-09-11"]
    assert local_date(ppi) < local_date(cpi)


# ----------------------------------------------------------------------
# 米雇用統計(JOBS)
# ----------------------------------------------------------------------
def test_jobs_december_crosses_into_next_january():
    """
    2026年12月分は翌年1月公表。第1金曜が 1/1(元日)なので後ろ倒しで 2027-01-08。
    旧実装は公表月ループが1〜12月で閉じており12月分を一度も生成せず、シードの
    幽霊行(2026-12-31)が残り続けていた。
    """
    ev = by_id(fs.build_us_jobs({2026}))["us_nfp_2026-12-31"]
    assert local_date(ev) == date(2027, 1, 8)
    assert ev["title"] == "米雇用統計 (2026年12月分)"


def test_jobs_december_stays_estimated():
    """
    BLSが2027年分の公式日程を未公表のうちは推定(True)のまま。
    確定(False)にすると notion_upsert の保護対象になり、公式公表後も更新されなくなる。
    """
    ev = by_id(fs.build_us_jobs({2026}))["us_nfp_2026-12-31"]
    assert ev["is_estimated"] is True


def test_jobs_source_url_is_bls_empsit():
    """
    全ての雇用統計行が BLS empsit を指すこと。
    シードの us_nfp_2026-12-31 には BEA(PCE) のURLが紛れ込んでいた。
    """
    for ev in fs.build_us_jobs({2026}):
        assert ev["source_url"] == "https://www.bls.gov/schedule/news_release/empsit.htm", ev["id"]
        assert "bea.gov" not in ev["source_url"]


def test_jobs_confirmed_months_are_not_estimated():
    """BLS公表済み範囲（11月分＝12/4まで）は確定扱いのままであること。"""
    ev = by_id(fs.build_us_jobs({2026}))["us_nfp_2026-12-04"]
    assert local_date(ev) == date(2026, 12, 4)
    assert ev["is_estimated"] is False


def test_jobs_july_holiday_pulls_forward():
    """独立記念日(7/3休)は前倒しで7/2。元日の後ろ倒しと向きが逆であることを固定する。"""
    ev = by_id(fs.build_us_jobs({2026}))["us_nfp_2026-07-02"]
    assert local_date(ev) == date(2026, 7, 2)


def test_no_duplicate_ids():
    """id重複が無いこと（重複すると notion_upsert が重複ページを作る）。"""
    events = fs.cpi_fallback() + fs.ppi_fallback() + fs.build_us_jobs({2026})
    ids = [e["id"] for e in events]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"id重複: {dupes}"


# ----------------------------------------------------------------------
# 真値表の期限切れ検知（本バグの再発防止装置そのもの）
# ----------------------------------------------------------------------
def test_warns_when_truth_table_runs_short(capsys):
    """
    真値表が将来の対象月をカバーしていなければ警告を出すこと。
    これが無いと、表が切れてもジョブは success のまま誤日付が残り続ける。
    """
    truncated = {k: v for k, v in fs.US_CPI_TRUTH.items() if k <= (2026, 6)}
    fs.warn_uncovered_targets("test[CPI]", "CPI", "US", truncated, today=date(2026, 7, 15))
    err = capsys.readouterr().err
    assert "::warning::" in err
    assert "2026-07" in err and "2026-11" in err


def test_no_warning_when_truth_table_is_current(capsys):
    """真値表が将来分を網羅していれば警告は出ないこと（オオカミ少年にしない）。"""
    fs.warn_uncovered_targets("test[CPI]", "CPI", "US", fs.US_CPI_TRUTH, today=date(2026, 7, 15))
    assert "::warning::" not in capsys.readouterr().err


def test_no_warning_for_already_published_months(capsys):
    """公表済みの過去月は今さら直せないので警告しないこと。"""
    truncated = {k: v for k, v in fs.US_CPI_TRUTH.items() if k != (2026, 1)}
    fs.warn_uncovered_targets("test[CPI]", "CPI", "US", truncated, today=date(2026, 7, 15))
    assert "::warning::" not in capsys.readouterr().err


def test_seed_alias_covers_all_truth_months():
    """
    真値表の対象月がシードのid エイリアスで全て解決できること。
    解決できない月は日付ベースの新規idになり、Notionに重複ページが生まれる。
    """
    for (tgt_y, tgt_m) in fs.US_CPI_TRUTH:
        assert ("CPI", "US", tgt_y, tgt_m) in fs._SEED_ID_ALIAS, f"CPI {tgt_y}-{tgt_m}"
    for (tgt_y, tgt_m) in fs.US_PPI_TRUTH:
        assert ("PPI", "US", tgt_y, tgt_m) in fs._SEED_ID_ALIAS, f"PPI {tgt_y}-{tgt_m}"
