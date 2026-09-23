"""
backtest_breadth.py の回帰テスト（ネットワーク不要・合成データ）。

守りたい性質:
  1. TradingView（UNIX 秒）/ Barchart（日付文字列・%付き）の CSV を同じ Series に読める
  2. 連続したシグナル日は 1 エピソードと数える（水増し防止）
  3. 先行リターン・最大下落率が未来の値だけで計算され、末尾は NaN（先読みなし）
  4. 条件 A/B/C/D が定義どおりに点灯し、データが無い日は点灯しない
  5. main() がダウンロード無しでも最後まで動き、CSV を出力する

実行:
    pip install -r requirements-dev.txt
    python -m pytest tests/test_backtest_breadth.py -v
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backtest_breadth as bb  # noqa: E402


def test_parse_tradingview_unix_seconds():
    csv = "time,open,high,low,close\n1758499200,50,51,49,50.49\n1756166400,72,73,71,72.11\n"
    s = bb.parse_series_csv(io.StringIO(csv), "BREADTH")
    assert list(s.index.strftime("%Y-%m-%d")) == ["2025-08-26", "2025-09-22"]
    assert s.iloc[-1] == pytest.approx(50.49)


@pytest.mark.parametrize("scale", [1, 10**3, 10**6, 10**9])
def test_parse_unix_time_any_resolution(scale):
    csv = f"time,close\n{1758499200 * scale},50.49\n"
    s = bb.parse_series_csv(io.StringIO(csv), "BREADTH")
    assert s.index[0] == pd.Timestamp("2025-09-22")


def test_parse_barchart_percent_strings():
    csv = "Date,Open,High,Low,Last,Change\n09/21/2026,50%,51%,49%,50.49%,-1\n08/24/2026,72%,73%,71%,72.11%,0\n"
    s = bb.parse_series_csv(io.StringIO(csv), "BREADTH")
    # Barchart は終値列が Last。最後の数値列（Change）を拾わないこと
    assert s.index[0] == pd.Timestamp("2026-08-24")
    assert list(s) == [72.11, 50.49]


def test_parse_cboe_style():
    csv = "DATE,COR1M\n09/16/2026,14.43\n09/15/2026,13.59\n"
    s = bb.parse_series_csv(io.StringIO(csv), "COR1M")
    assert s.loc["2026-09-16"] == pytest.approx(14.43)
    assert s.index.is_monotonic_increasing


def test_normalize_breadth_fraction():
    s = pd.Series([0.72, 0.50])
    assert list(bb.normalize_breadth(s)) == [72.0, 50.0]
    assert list(bb.normalize_breadth(pd.Series([72.0]))) == [72.0]


def test_episode_starts_dedups_consecutive_days():
    sig = pd.Series([False, True, True, True, False, False, True, False] + [False] * 5 + [True])
    starts = bb.episode_starts(sig, cooldown=3)
    # idx1 が新規、2-3 は継続、6 は直前3日(3,4,5)に idx3 があるので継続、13 は新規
    assert list(starts[starts].index) == [1, 13]


def test_forward_metrics_no_lookahead():
    idx = pd.bdate_range("2020-01-01", periods=300)
    spx = pd.Series(np.linspace(100, 130, 300), index=idx)
    spx.iloc[100] = 80  # 一時的な急落
    fwd = bb.forward_metrics(spx)
    assert fwd["ret_21d"].iloc[0] == pytest.approx(spx.iloc[21] / spx.iloc[0] - 1)
    assert fwd["ret_21d"].iloc[-21:].isna().all()
    # 急落の 63 日以内の起点は -10% 以上の下落を検出、急落日そのものは起点に含めない
    assert fwd["hit_dd10"].iloc[99] == 1.0
    assert fwd[f"maxdd_{bb.DD_WINDOW}d"].iloc[100] > 0
    assert fwd["hit_dd10"].iloc[-bb.DD_WINDOW:].isna().all()


def _args(**kw):
    a = bb.parse_args([])
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_build_conditions_definitions():
    idx = pd.bdate_range("2024-01-01", periods=300)
    spx = pd.Series(np.linspace(100, 200, 300), index=idx)  # 常に高値更新
    breadth = pd.Series(72.0, index=idx)
    breadth.iloc[280:] = 50.49 - np.arange(20) * 0.1  # 20日で 72 → 50 割れ
    cor = pd.Series(8.5, index=idx)
    cor.iloc[-1] = 15.5
    df = pd.DataFrame({"SPX": spx, "BREADTH": breadth, "COR1M": cor})
    df.loc[idx[:50], "COR1M"] = np.nan
    cond = bb.build_conditions(df, _args())

    assert not cond["B"].iloc[:251].any()  # 252日分そろうまで高値判定しない
    assert cond["B"].iloc[260:].all()
    assert cond["A"].iloc[-1] and not cond["A"].iloc[270]
    assert cond["D"].iloc[-1] and not cond["D"].iloc[279]
    assert cond["L"].iloc[100] and not cond["L"].iloc[10]  # NaN の日は点灯しない
    assert cond["C"].iloc[-1] and not cond["C"].iloc[-2]


def test_main_runs_offline(tmp_path, monkeypatch):
    idx = pd.bdate_range("2015-01-01", periods=1500)
    rng = np.random.default_rng(1)
    spx = pd.Series(1000 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, len(idx)))), index=idx)
    vix = pd.Series(15 + rng.normal(0, 3, len(idx)).cumsum() % 20, index=idx)

    def fake_yf(ticker, start):
        return {"^GSPC": spx, "^VIX": vix}[ticker].rename(ticker)

    monkeypatch.setattr(bb, "fetch_yf", fake_yf)
    breadth = pd.DataFrame({"Date": idx.strftime("%Y-%m-%d"),
                            "Close": 60 + 20 * np.sin(np.arange(len(idx)) / 30)})
    cor = pd.DataFrame({"DATE": idx.strftime("%m/%d/%Y"),
                        "COR1M": 15 + 7 * np.sin(np.arange(len(idx)) / 25)})
    bpath, cpath = tmp_path / "s5th.csv", tmp_path / "cor1m.csv"
    breadth.to_csv(bpath, index=False)
    cor.to_csv(cpath, index=False)

    out = tmp_path / "out"
    rc = bb.main(["--breadth", str(bpath), "--cor1m-csv", str(cpath),
                  "--out", str(out), "--iter", "200", "--start", "2015-01-01"])
    assert rc == 0
    summary = pd.read_csv(out / "summary.csv", encoding="utf-8-sig")
    assert {"A&B", "L&A&B", "C", "V"} <= set(summary["条件"])
    assert (out / "episodes.csv").exists()


def test_summarize_separates_unresolved_episodes():
    """直近の点灯（63日後がまだ無い）は 件数 に入るが 評価済件数 には入らない。"""
    idx = pd.bdate_range("2020-01-01", periods=300)
    spx = pd.Series(np.linspace(100, 130, 300), index=idx)
    fwd = bb.forward_metrics(spx)
    starts = pd.Series(False, index=idx)
    starts.iloc[[10, 100, 290]] = True  # 290 は末尾から10日目＝未確定
    universe = pd.Series(True, index=idx)
    row = bb.summarize(fwd, starts, universe, 50, np.random.default_rng(0))
    assert row["件数"] == 3
    assert row["評価済件数"] == 2
