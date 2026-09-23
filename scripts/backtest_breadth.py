"""
S&P500「内部悪化シグナル」のバックテスト（PC 側で手動実行する研究用スクリプト）。

検証する主張（2026-09 に出回った図解より）:
  - 200日線上の銘柄比率が 1ヶ月で 72% → 50% に急低下（breadth 悪化）
  - 指数は高値圏のまま（negative breadth divergence）
  - COR1M（1ヶ月インプライド相関）が 8〜9 の超低水準 → 15〜20 へ急反転したら要警戒
  → これらのシグナル後、S&P500 は本当に平常時より悪いのか？

判定条件（結果を見る前に固定する。CLI 引数で変更可）:
  A  breadth 急低下   : 200日線上比率が 20営業日で 15pt 以上低下
  B  指数高値圏       : S&P500 終値が 252営業日高値から 3% 以内
  D  breadth 50% 割れ : 200日線上比率 < 50
  L  超低相関         : COR1M <= 10
  C  相関の急反転     : COR1M >= 15 かつ 直近20営業日の最小値 <= 10
  V  VIX 上昇         : VIX が 20営業日前より 30% 以上高い

評価（シグナル日＝エピソード初日の終値から）:
  - 21/63/126営業日後リターン（≒1/3/6ヶ月）
  - 63営業日以内の最大下落率、その間に -10% 以上の下落が起きた率
  - 同じデータ期間の「全営業日」を基準線とし、差と並べ替え検定の p 値を出す
  - 連続するシグナル日は 1 回と数える（直前 cooldown 日にシグナルがあれば新規扱いしない）

データ:
  - S&P500 (^GSPC)・VIX (^VIX) : yfinance
  - COR1M : Cboe 公式 CSV（失敗時 yfinance ^COR1M）
  - 200日線上比率 : 自動取得できない。TradingView / Barchart 等から S5TH の日足履歴を
    CSV で書き出し --breadth で渡す（無ければ breadth 系の条件はスキップ）。
    ※ 現在の構成銘柄だけで自作すると除外銘柄が抜けて過去の breadth が良く見える
      （生存者バイアス）。自作するなら時点ごとの構成銘柄を使うこと。

実行例:
    pip install -r requirements.txt
    python scripts/backtest_breadth.py --breadth S5TH.csv
    python scripts/backtest_breadth.py --breadth S5TH.csv --start 2000-01-01 --out backtest_out

出力:
  - 画面: 条件別の集計表（基準線との比較・p値）、直近の各指標と条件の点灯状況
  - <out>/summary.csv, <out>/episodes.csv（Excel で開けるよう UTF-8 BOM 付き）

限界（結果の読み方）:
  - 該当エピソードは数件〜20件程度しかなく、1 回の大暴落で平均が大きく動く
  - 63/126日の先行リターンは互いに重なるので p 値は甘めに出る。傾向の目安として読む
  - 「下落開始の確定」ではなく「平常時と比べた確率の偏り」を見るもの
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HORIZONS = (21, 63, 126)
DD_WINDOW = 63
DD_THRESHOLD = -0.10
COR1M_CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/COR1M_History.csv"

DATE_COLS = ("date", "time", "timestamp", "datetime", "日付")
VALUE_COLS = ("close", "last", "adj close", "value", "price", "終値", "cor1m", "s5th")


# ---------------------------------------------------------------- データ読み込み

def parse_series_csv(src, name: str) -> pd.Series:
    """日付列＋値列を持つ CSV を日付 index の Series にする。

    TradingView（time が UNIX 秒）/ Barchart / Investing.com / Cboe の書き出しを想定し、
    列名は大文字小文字を無視して推測する。値列が見つからなければ最後の数値列を使う。
    """
    df = pd.read_csv(src, thousands=",")
    cols = {c.strip().lower(): c for c in df.columns}
    date_col = next((cols[c] for c in DATE_COLS if c in cols), df.columns[0])
    value_col = next((cols[c] for c in VALUE_COLS if c in cols), None)
    if value_col is None:
        numeric = [c for c in df.columns if c != date_col
                   and pd.to_numeric(df[c], errors="coerce").notna().mean() > 0.9]
        if not numeric:
            raise ValueError(f"{name}: 値の列が見つかりません（列: {list(df.columns)}）")
        value_col = numeric[-1]

    raw_dates = df[date_col]
    if pd.api.types.is_numeric_dtype(raw_dates):
        # UNIX 時刻。桁数で秒/ミリ秒/マイクロ秒/ナノ秒を見分ける
        mag = raw_dates.abs().median()
        unit = "s" if mag < 1e11 else "ms" if mag < 1e14 else "us" if mag < 1e17 else "ns"
        dates = pd.to_datetime(raw_dates, unit=unit)
    else:
        dates = pd.to_datetime(raw_dates.astype(str).str.strip(), errors="coerce")
    if getattr(dates.dt, "tz", None) is not None:
        dates = dates.dt.tz_localize(None)
    values = pd.to_numeric(
        df[value_col].astype(str).str.replace("%", "", regex=False), errors="coerce")

    s = pd.Series(values.to_numpy(), index=dates.dt.normalize(), name=name)
    s = s[s.index.notna()].dropna()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if s.empty:
        raise ValueError(f"{name}: 有効な行がありません")
    return s


def normalize_breadth(s: pd.Series) -> pd.Series:
    """0〜1 表記なら 0〜100 に揃える。"""
    return s * 100 if s.max() <= 1.0 else s


def fetch_yf(ticker: str, start: str) -> pd.Series:
    import yfinance as yf
    df = yf.download(ticker, start=start, auto_adjust=False, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"yfinance: {ticker} を取得できませんでした")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):  # yfinance の MultiIndex 列
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.dropna().rename(ticker)


def fetch_cor1m(start: str, local_csv: str | None) -> pd.Series:
    if local_csv:
        return parse_series_csv(local_csv, "COR1M")
    try:
        import requests
        r = requests.get(COR1M_CBOE_URL, timeout=30,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        s = parse_series_csv(io.StringIO(r.text), "COR1M")
        print(f"COR1M: Cboe 公式 CSV から取得（{s.index[0].date()}〜{s.index[-1].date()}）")
    except Exception as e:  # noqa: BLE001
        print(f"COR1M: Cboe CSV 取得失敗（{e}）→ yfinance ^COR1M を試します")
        s = fetch_yf("^COR1M", start).rename("COR1M")
    return s[s.index >= pd.Timestamp(start)]


# ---------------------------------------------------------------- シグナルと評価

def build_conditions(df: pd.DataFrame, args) -> dict[str, pd.Series]:
    """各条件を bool Series で返す。データが無い期間は False（NaN 比較は False）。"""
    cond: dict[str, pd.Series] = {}
    spx = df["SPX"]
    cond["B"] = spx >= spx.rolling(252, min_periods=252).max() * (1 - args.near_high)
    if "VIX" in df:
        cond["V"] = df["VIX"] >= df["VIX"].shift(20) * (1 + args.vix_jump)
    if "BREADTH" in df:
        b = df["BREADTH"]
        cond["A"] = b.diff(args.breadth_window) <= -args.breadth_drop
        cond["D"] = b < args.breadth_floor
    if "COR1M" in df:
        c = df["COR1M"]
        cond["L"] = c <= args.cor_low
        cond["C"] = (c >= args.cor_high) & (c.rolling(20, min_periods=20).min() <= args.cor_low)
    return {k: v.fillna(False).astype(bool) for k, v in cond.items()}


COMBOS = [
    ("B", "指数高値圏（参考: 平常時に近い）"),
    ("A", "breadth 20日で急低下"),
    ("D", "breadth 50%割れ"),
    ("A&B", "breadth 急低下 × 指数高値圏（図解の本題）"),
    ("A&B&D", "上記 ＋ 50%割れ"),
    ("L", "COR1M 超低水準"),
    ("L&A&B", "超低相関 × breadth 急低下 × 高値圏"),
    ("C", "COR1M 急反転（10以下→15以上）"),
    ("C&D", "COR1M 急反転 × breadth 50%割れ"),
    ("V", "VIX 20日で30%上昇"),
    ("A&V", "breadth 急低下 × VIX 上昇"),
]


def combine(cond: dict[str, pd.Series], expr: str) -> pd.Series | None:
    keys = expr.split("&")
    if any(k not in cond for k in keys):
        return None
    out = cond[keys[0]].copy()
    for k in keys[1:]:
        out &= cond[k]
    return out


def episode_starts(signal: pd.Series, cooldown: int) -> pd.Series:
    """シグナル日のうち、直前 cooldown 営業日にシグナルが無かった日だけ True。"""
    prior = signal.shift(1, fill_value=False).astype(int).rolling(cooldown, min_periods=1).max()
    return signal & (prior == 0)


def forward_metrics(spx: pd.Series) -> pd.DataFrame:
    """各日を起点にした将来リターンと最大下落率。データ末尾で足りない分は NaN。"""
    out = pd.DataFrame(index=spx.index)
    for h in HORIZONS:
        out[f"ret_{h}d"] = spx.shift(-h) / spx - 1
    arr = spx.to_numpy(dtype=float)
    n = len(arr)
    mdd = np.full(n, np.nan)
    for i in range(n - DD_WINDOW):
        mdd[i] = arr[i + 1:i + 1 + DD_WINDOW].min() / arr[i] - 1
    out[f"maxdd_{DD_WINDOW}d"] = mdd
    out["hit_dd10"] = np.where(np.isnan(mdd), np.nan, (mdd <= DD_THRESHOLD).astype(float))
    return out


def permutation_p(sample: np.ndarray, population: np.ndarray, n_iter: int,
                  rng: np.random.Generator) -> float:
    """「シグナル日の平均が、同数を無作為に選んだ平均より低い」片側 p 値。"""
    if len(sample) == 0 or len(population) < len(sample):
        return float("nan")
    obs = sample.mean()
    draws = rng.choice(population, size=(n_iter, len(sample)), replace=True).mean(axis=1)
    return float((np.sum(draws <= obs) + 1) / (n_iter + 1))


def summarize(fwd: pd.DataFrame, starts: pd.Series, universe: pd.Series,
              n_iter: int, rng: np.random.Generator) -> dict:
    """universe（その条件が評価可能な日）を基準線にして比較する。"""
    base = fwd[universe]
    ev = fwd[starts & universe]
    # 件数 は直近の未確定エピソードも含む。結果が出ているのは 評価済件数（63日後が確定した分）
    row = {"件数": int(len(ev)), "評価済件数": int(ev[f"maxdd_{DD_WINDOW}d"].notna().sum())}
    for h in HORIZONS:
        col = f"ret_{h}d"
        e, b = ev[col].dropna().to_numpy(), base[col].dropna().to_numpy()
        row[f"{h}日後平均%"] = e.mean() * 100 if len(e) else np.nan
        row[f"{h}日後_基準%"] = b.mean() * 100 if len(b) else np.nan
        row[f"{h}日後_上昇率%"] = (e > 0).mean() * 100 if len(e) else np.nan
        row[f"{h}日後_p値"] = permutation_p(e, b, n_iter, rng)
    col = f"maxdd_{DD_WINDOW}d"
    e, b = ev[col].dropna().to_numpy(), base[col].dropna().to_numpy()
    row["最大下落_平均%"] = e.mean() * 100 if len(e) else np.nan
    row["最大下落_基準%"] = b.mean() * 100 if len(b) else np.nan
    e, b = ev["hit_dd10"].dropna().to_numpy(), base["hit_dd10"].dropna().to_numpy()
    row["10%下落率%"] = e.mean() * 100 if len(e) else np.nan
    row["10%下落_基準%"] = b.mean() * 100 if len(b) else np.nan
    return row


def run_backtest(df: pd.DataFrame, args) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    cond = build_conditions(df, args)
    fwd = forward_metrics(df["SPX"])
    rng = np.random.default_rng(args.seed)
    rows, episodes = [], []
    for expr, label in COMBOS:
        sig = combine(cond, expr)
        if sig is None:
            continue
        needed = {"A": "BREADTH", "D": "BREADTH", "L": "COR1M", "C": "COR1M", "V": "VIX"}
        cols = {needed[k] for k in expr.split("&") if k in needed} | {"SPX"}
        universe = df[list(cols)].notna().all(axis=1)
        starts = episode_starts(sig, args.cooldown)
        rows.append({"条件": expr, "内容": label, **summarize(fwd, starts, universe, args.iter, rng)})
        for d in starts[starts & universe].index:
            episodes.append({"条件": expr, "日付": d.date(), "SPX": df.at[d, "SPX"],
                             **{k: df.at[d, k] for k in ("BREADTH", "COR1M", "VIX") if k in df},
                             **fwd.loc[d].to_dict()})
    return pd.DataFrame(rows), pd.DataFrame(episodes), cond


# ---------------------------------------------------------------- 表示

def print_summary(summary: pd.DataFrame) -> None:
    if summary.empty:
        print("評価できる条件がありません（データ不足）")
        return
    show = ["条件", "件数", "評価済件数"]
    for h in HORIZONS:
        show += [f"{h}日後平均%", f"{h}日後_基準%", f"{h}日後_p値"]
    show += ["最大下落_平均%", "最大下落_基準%", "10%下落率%", "10%下落_基準%"]
    with pd.option_context("display.width", 250, "display.max_columns", None,
                           "display.float_format", "{:.2f}".format):
        print(summary[show].to_string(index=False))
    print("\n条件の内容:")
    for _, r in summary.iterrows():
        print(f"  {r['条件']:<8} {r['内容']}")
    print("\n読み方: 基準% はその条件を評価できた全営業日の平均。p値 は「シグナル後の平均が"
          "無作為抽出より低い」片側検定（小さいほど弱気シグナルとして有意）。"
          "評価済件数 が 10 未満の行は参考程度に（件数 には結果が出る前の直近エピソードも入る）。")


def print_latest(df: pd.DataFrame, cond: dict[str, pd.Series]) -> None:
    print("\n=== 直近の状態 ===")
    for col in ("SPX", "BREADTH", "COR1M", "VIX"):
        if col in df:
            s = df[col].dropna()
            if s.empty:
                print(f"  {col:<8} {'—':>10}  （期間内のデータなし）")
                continue
            print(f"  {col:<8} {s.iloc[-1]:>10.2f}  （{s.index[-1].date()} 終値）")
    last = df.index[-1]
    lit = [k for k, v in cond.items() if bool(v.get(last, False))]
    print(f"  {last.date()} に点灯中の条件: {', '.join(sorted(lit)) or 'なし'}")


# ---------------------------------------------------------------- main

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="S&P500 内部悪化シグナルのバックテスト")
    p.add_argument("--breadth", help="200日線上比率（S5TH）の日足 CSV")
    p.add_argument("--cor1m-csv", help="COR1M の日足 CSV（指定時はダウンロードしない）")
    p.add_argument("--start", default="1995-01-01", help="取得開始日")
    p.add_argument("--out", default="backtest_out", help="CSV 出力先フォルダ")
    p.add_argument("--breadth-window", type=int, default=20)
    p.add_argument("--breadth-drop", type=float, default=15.0, help="A: 低下幅(pt)")
    p.add_argument("--breadth-floor", type=float, default=50.0, help="D: 割れ水準(%)")
    p.add_argument("--near-high", type=float, default=0.03, help="B: 高値からの距離")
    p.add_argument("--cor-low", type=float, default=10.0, help="L/C: 低相関の閾値")
    p.add_argument("--cor-high", type=float, default=15.0, help="C: 急反転の閾値")
    p.add_argument("--vix-jump", type=float, default=0.30, help="V: 20日上昇率")
    p.add_argument("--cooldown", type=int, default=20, help="同一エピソードとみなす営業日数")
    p.add_argument("--iter", type=int, default=5000, help="並べ替え検定の回数")
    p.add_argument("--seed", type=int, default=0)
    return p.parse_args(argv)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows コンソールの文字化け対策
    args = parse_args(argv)

    spx = fetch_yf("^GSPC", args.start).rename("SPX")
    print(f"S&P500: {spx.index[0].date()}〜{spx.index[-1].date()}（{len(spx)}日）")
    frame = {"SPX": spx}
    try:
        frame["VIX"] = fetch_yf("^VIX", args.start).rename("VIX")
    except Exception as e:  # noqa: BLE001
        print(f"VIX: 取得失敗（{e}）→ V 条件はスキップ")
    try:
        frame["COR1M"] = fetch_cor1m(args.start, args.cor1m_csv)
    except Exception as e:  # noqa: BLE001
        print(f"COR1M: 取得失敗（{e}）→ L/C 条件はスキップ")
    if args.breadth:
        b = normalize_breadth(parse_series_csv(args.breadth, "BREADTH"))
        print(f"breadth: {b.index[0].date()}〜{b.index[-1].date()}（{len(b)}日）")
        overlap = b.index.isin(spx.index).sum()
        if overlap < 252:
            print(f"  ⚠ S&P500 と日付が重なるのは {overlap} 日だけです。CSV の日付列を確認してください")
        frame["BREADTH"] = b
    else:
        print("breadth: --breadth 未指定 → A/D 条件はスキップ")

    # S&P500 の営業日に揃える（他系列の欠損日は前日値で埋めない＝その日は評価対象外）
    df = pd.DataFrame(frame).loc[spx.index]

    summary, episodes, cond = run_backtest(df, args)
    print("\n=== 条件別の集計（S&P500 のその後） ===")
    print_summary(summary)
    print_latest(df, cond)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary.to_csv(out / "summary.csv", index=False, encoding="utf-8-sig")
    episodes.to_csv(out / "episodes.csv", index=False, encoding="utf-8-sig")
    print(f"\n出力: {out / 'summary.csv'} / {out / 'episodes.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
