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


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """
    公式日程の控えファイルをテストごとの一時ファイルに差し替える。
    本物の data/official_schedule_cache.json を読むと結果が日々変わり、書くと汚れるため。
    """
    path = tmp_path / "official_schedule_cache.json"
    monkeypatch.setattr(fs, "SCHEDULE_CACHE_FILE", path)
    return path


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


def _trim_cpi_seed_to_truth():
    """
    シードの CPI 対象月を、真値表の最終月までに絞る。
    2026-10 から CPI は BLS 公式を直接取得しており、シードは真値表より先の月
    （2026-12〜）まで自動で伸びる。CPI 真値表は年次追記をやめたので、絞らないと
    「網羅しているはず」の前提がデータの伸びだけで崩れてテストが落ちる。
    """
    last = max(fs.US_CPI_TRUTH)
    fs._SEED_ID_ALIAS = {
        k: v for k, v in fs._SEED_ID_ALIAS.items()
        if not (k[0] == "CPI" and k[1] == "US" and (k[2], k[3]) > last)
    }


def test_no_warning_when_truth_table_is_current(capsys):
    """真値表が将来分を網羅していれば警告は出ないこと（オオカミ少年にしない）。"""
    _trim_cpi_seed_to_truth()
    fs.warn_uncovered_targets("test[CPI]", "CPI", "US", fs.US_CPI_TRUTH, today=date(2026, 7, 15))
    assert "::warning::" not in capsys.readouterr().err


def test_no_warning_for_already_published_months(capsys):
    """公表済みの過去月は今さら直せないので警告しないこと。"""
    _trim_cpi_seed_to_truth()
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


# ── 真値表の残り月数 警告（2026-08-22 追加）────────────────────────────
def test_truth_runway_warns_when_under_two_months():
    """最終対象月 2026-11 に対し今日が 2026-10 なら残り1か月 → 警告する"""
    truth = {(2026, 10): date(2026, 11, 10), (2026, 11): date(2026, 12, 10)}
    assert fs.warn_truth_runway("test[CPI]", truth, today=date(2026, 10, 1)) is True


def test_truth_runway_quiet_when_enough():
    """最終対象月 2026-11 に対し今日が 2026-08 なら残り3か月 → 鳴らない"""
    truth = {(2026, 10): date(2026, 11, 10), (2026, 11): date(2026, 12, 10)}
    assert fs.warn_truth_runway("test[CPI]", truth, today=date(2026, 8, 22)) is False


def test_truth_runway_handles_gdp_quarter_keys():
    """GDP の (年, 四半期, 種別) キーは四半期末月に換算する（Q2→6月）"""
    truth = {(2026, 2, "advance"): date(2026, 7, 30)}
    assert fs.warn_truth_runway("test[GDP]", truth, today=date(2026, 3, 1)) is False   # 残り3か月
    assert fs.warn_truth_runway("test[GDP]", truth, today=date(2026, 5, 1)) is True    # 残り1か月


def test_truth_runway_runs_on_real_tables():
    """本番の5つの真値表に対して例外なく動くこと（切れかけの警告自体はCIを落とさない。
    実運用では sync の ::warning:: → health-watchdog の注釈監視 → LINE で届く）"""
    for name in ("US_CPI_TRUTH", "US_PPI_TRUTH", "US_PCE_TRUTH", "US_RETAIL_TRUTH", "US_GDP_TRUTH"):
        assert fs.warn_truth_runway(f"test[{name}]", getattr(fs, name), today=date(2026, 8, 22)) in (True, False)


# ----------------------------------------------------------------------
# BLS 公式ページの解析（2026-10-06 追加）
#   BLS は名乗りに連絡先メールを入れれば 200 で読める（403 は海外IP遮断ではなかった）。
#   ところが解析部は本物のページで一度も動いておらず、2つのバグがあった:
#     ① 表は 対象月|公表日|公表時刻 の3列なのに最後の列（時刻）を公表日として読み、0件
#     ② 左メニュー（"FEBRUARY 2026" 等）を含む外枠の行を拾い、ゴミ行が1件できる
#   実ページの構造を縮めた HTML で両方を固定する。
# ----------------------------------------------------------------------
_BLS_PAGE = """
<table><tr>
<td id="secondary-nav-td"><ul><li><a>FEBRUARY 2026</a></li></ul></td>
<td><table>
<tr><th>Reference Month</th><th>Release Date</th><th>Release Time</th></tr>
<tr class="release-list-even-row"><td>November 2026</td><td>Dec. 10, 2026</td><td>08:30 AM</td></tr>
<tr class="release-list-odd-row"><td>December 2026</td><td>Jan. 13, 2027</td><td>08:30 AM</td></tr>
</table></td>
</tr></table>
"""


class _FakeResp:
    status_code = 200
    text = _BLS_PAGE


@pytest.mark.parametrize("fn,cat", [("fetch_bls_cpi", "CPI"), ("fetch_bls_ppi", "PPI")])
def test_bls_official_page_parse(monkeypatch, fn, cat):
    monkeypatch.setattr(fs.requests, "get", lambda *a, **k: _FakeResp())
    events = getattr(fs, fn)()
    got = sorted(e["datetime_local"][:10] for e in events)
    # 公表日の列（2列目）を読み、メニュー行のゴミが混ざらないこと
    assert got == ["2026-12-10", "2027-01-13"]
    assert all(e["category"] == cat and e["is_estimated"] is False for e in events)
    # 年をまたぐ対象月（2026年12月分 → 2027年1月公表）は年付きの題になる
    assert any("2026年12月分" in e["title"] for e in events)


def test_bls_user_agent_has_no_url():
    # URL を含む名乗りは BLS に 403 で弾かれる（2026-10-06 実測）。
    # 連絡先が設定されているときの名乗りに URL を混ぜないこと。
    import importlib, os
    os.environ["BLS_CONTACT_EMAIL"] = "test@example.com"
    try:
        m = importlib.reload(fs)
        assert m.BLS_USER_AGENT == "investor-calendar-bot/1.0 (test@example.com)"
        assert "http" not in m.BLS_USER_AGENT
    finally:
        del os.environ["BLS_CONTACT_EMAIL"]
        importlib.reload(fs)


# ----------------------------------------------------------------------
# 米小売売上高: Census の日程ページは速報版の表だけを読む（2026-10-22）
#   同じページの確報版の表まで読むと、同じ対象月が1か月遅い日付で上書きされ
#   「◯月分」が1か月ずれる。実ページの抜粋（固定データ）で確かめる。
# ----------------------------------------------------------------------
CENSUS_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "census_release_schedule_2026-10-22.html"


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


def _census_events(monkeypatch) -> dict[str, dict]:
    html = CENSUS_FIXTURE.read_text(encoding="utf-8")
    monkeypatch.setattr(fs.requests, "get", lambda *a, **k: _FakeResponse(html))
    return by_id(fs.fetch_census_retail())


def test_census_reads_only_advance_table(monkeypatch):
    """速報版の表だけを読み、対象月と公表日の組が速報版どおりになること。"""
    ev = _census_events(monkeypatch)
    assert local_date(ev["us_retail_2026-09"]) == date(2026, 10, 15)
    assert local_date(ev["us_retail_2026-10"]) == date(2026, 11, 17)
    assert local_date(ev["us_retail_2026-12"]) == date(2027, 1, 15)
    assert local_date(ev["us_retail_2027-11"]) == date(2027, 12, 15)
    # 確報版にしか無い 2026年8月分（10/15公表）を拾っていないこと
    assert "us_retail_2026-08" not in ev


def test_census_without_advance_table_falls_back(monkeypatch):
    """速報版の表が見つからなければ、確報版を読まずに真値表へ切り替えること。"""
    html = CENSUS_FIXTURE.read_text(encoding="utf-8").replace("Advance Monthly Retail", "Something Else")
    monkeypatch.setattr(fs.requests, "get", lambda *a, **k: _FakeResponse(html))
    ev = by_id(fs.fetch_census_retail())
    assert local_date(ev["us_retail_2026-10"]) == fs.US_RETAIL_TRUTH[(2026, 10)]


# ----------------------------------------------------------------------
# 公式日程の控え（自動保存）: 公式が取れた日に貯め、取れない日のフォールバックに使う（2026-10-22）
# ----------------------------------------------------------------------
def test_cache_extends_fallback_beyond_truth_table():
    """控えに入った先の月が、真値表に無くてもフォールバックに出ること。"""
    n = fs.save_official_dates("PCE", [((2027, 1), date(2027, 2, 26))], today=date(2026, 12, 1))
    assert n == 1
    ev = [e for e in fs.pce_fallback() if e["title"].endswith("(1月分)")]
    assert [local_date(e) for e in ev] == [date(2027, 2, 26)]


def test_cache_overrides_truth_table():
    """同じ対象月は公式由来の控えを真値表より優先すること。"""
    fs.save_official_dates("RETAIL", [((2026, 11), date(2026, 12, 17))], today=date(2026, 11, 1))
    assert fs.fallback_table("RETAIL", fs.US_RETAIL_TRUTH)[(2026, 11)] == date(2026, 12, 17)


def test_cache_keeps_keys_missing_from_later_fetch():
    """次の取得に載っていない月も控えから消さない（BEA は数か月先しか載せない）。"""
    fs.save_official_dates("GDP", [((2026, 3, "adv"), date(2026, 10, 29))], today=date(2026, 10, 1))
    fs.save_official_dates("GDP", [((2026, 4, "adv"), date(2027, 1, 28))], today=date(2026, 12, 1))
    cached = fs.cached_dates("GDP")
    assert cached[(2026, 3, "adv")] == date(2026, 10, 29)
    assert cached[(2026, 4, "adv")] == date(2027, 1, 28)


def test_cache_rejects_two_dates_for_one_key(isolated_cache):
    """1回の取得で同じ月に日付が2つ出たら（表の取り違え）何も保存しないこと。"""
    n = fs.save_official_dates("RETAIL", [((2026, 9), date(2026, 10, 15)),
                                          ((2026, 9), date(2026, 11, 17))], today=date(2026, 10, 1))
    assert n == 0
    assert not isolated_cache.exists()


def test_cache_rejects_implausible_lag():
    """期末より前や、期末から離れすぎた公表日は読み違いとみなして保存しないこと。"""
    n = fs.save_official_dates("CPI", [((2026, 10), date(2026, 10, 5)),     # 期末より前
                                       ((2026, 11), date(2027, 3, 1)),      # 離れすぎ
                                       ((2026, 12), date(2027, 1, 13))],    # 正常
                               today=date(2026, 10, 1))
    assert n == 1
    assert set(fs.cached_dates("CPI")) == {(2026, 12)}


def test_cache_prunes_old_entries():
    """公表日が CACHE_KEEP_DAYS より古い控えは捨てること。"""
    fs.save_official_dates("CPI", [((2025, 1), date(2025, 2, 12))], today=date(2025, 1, 15))
    fs.save_official_dates("CPI", [((2026, 12), date(2027, 1, 13))], today=date(2026, 12, 1))
    assert set(fs.cached_dates("CPI")) == {(2026, 12)}


def test_broken_cache_file_does_not_stop_fallback(isolated_cache):
    """控えが壊れていても、真値表だけでフォールバックは作れること。"""
    isolated_cache.write_text("{壊れたJSON", encoding="utf-8")
    assert len(fs.retail_fallback()) == len(fs.US_RETAIL_TRUTH)


def test_census_fetch_fills_cache_then_fallback_uses_it(monkeypatch):
    """
    実ページ抜粋で公式取得 → 控えに速報版15件 → 翌日 Census が 500 でも
    控えから 2027年11月分まで出て、残り期間の警告も出ないこと（今日の WARN の再発防止）。
    """
    _census_events(monkeypatch)
    cached = fs.cached_dates("RETAIL")
    assert len(cached) == 15 and cached[(2026, 10)] == date(2026, 11, 17)
    monkeypatch.setattr(fs.requests, "get", lambda *a, **k: _FakeResponse("", status_code=500))
    ev = by_id(fs.fetch_census_retail())
    assert local_date(ev["us_retail_2027-11"]) == date(2027, 12, 15)
    assert fs.warn_truth_runway("t", fs.fallback_table("RETAIL", {}), today=date(2026, 10, 22)) is False


def test_ppi_fallback_months_follow_cache():
    """PPI のフォールバックの対象月が、2026年11月分の固定でなく控えに合わせて伸びること。"""
    fs.save_official_dates("PPI", [((2027, 3), date(2027, 4, 14))], today=date(2027, 1, 1))
    ids = by_id(fs.ppi_fallback())
    assert any(local_date(e) == date(2027, 4, 14) and e["is_estimated"] is False for e in ids.values())
