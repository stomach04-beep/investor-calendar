# -*- coding: utf-8 -*-
"""
決算日の予測がどれだけ当たるかを、過去データで測る（アウトオブサンプル検証）。

背景:
  カレンダーに載る決算日は、日付の出どころが4種類ある。
    JPX公式 / Nasdaq公式 … 取引所の発表予定＝ほぼ確定。ただし約1ヶ月〜5週先までしか出ない
    yfinance             … Yahoo の予定日（推定を含む）。過去の推定値が残らないので検証できない
    JQ予測 / EDGAR予測   … その銘柄の過去の発表実績から作る予測 ← ここを本スクリプトで測る
  公式の圏外（＝1ヶ月より先）はこの予測が最後の砦なので、
  「ピタリ何%／±1日で何%」を数字で押さえておく。

やり方（未来のデータを使わない）:
  ある実発表日 D について、D より前の発表履歴だけを使って予測を作り、D と突き合わせる。
  予測ルールは本番と同じ fetch_earnings.next_from_history（昨年同四半期の実発表日 +364日）。

データ源:
  日本株 … data/jq_earnings_jp.json を作るのと同じ J-Quants 開示履歴
           （既定では jquants-bulk/data/*.json を探す。--jq-history で明示指定も可）
  米国株 … SEC EDGAR の 8-K(Item 2.02) 実績（--symbols で銘柄を指定）

実行:
  python scripts/earnings_accuracy.py --us --symbols AAPL,KO,VZ,HD
  python scripts/earnings_accuracy.py --jp --jq-history <開示履歴JSON>
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import log  # noqa: E402
from fetch_earnings import next_from_history  # noqa: E402
from fetch_earnings import pick_next_date as next_pick  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# 何日ズレたかの集計区分
BUCKETS = [(0, "ピタリ"), (1, "±1日"), (3, "±3日"), (7, "±7日")]


def evaluate(series: dict[str, list[str]], min_history: int = 4,
             rule: str = "anchored") -> dict:
    """{銘柄: 発表日リスト(古い順)} を受け取り、予測の当たり具合を返す。

    各発表日について「それ以前の履歴だけ」で予測を作るので、
    未来を覗いた検証（ルックアヘッド）にはならない。
    """
    diffs: list[int] = []
    n_skip = 0
    for _code, days in series.items():
        ds = sorted({str(d)[:10] for d in days if d})
        for i, actual_s in enumerate(ds):
            past = ds[:i]                      # ← その時点で分かっていた履歴だけ
            if len(past) < min_history:
                n_skip += 1
                continue
            actual = date.fromisoformat(actual_s)
            # 「予測を作った日」は前回発表の翌日（実運用でも決算通過直後に次を出す）。
            # 前回発表日そのものを起点にすると「1年前の同じ四半期+364日＝前回発表日」が
            # 候補に残り、3ヶ月手前の日付を予測として選んでしまう。
            asof = date.fromisoformat(past[-1]) + timedelta(days=1)
            pred = next_from_history(list(reversed(past)), asof, rule=rule)
            if pred is None:
                n_skip += 1
                continue
            diffs.append((pred - actual).days)
    total = len(diffs)
    out = {"n": total, "skipped": n_skip, "buckets": {}, "bias": None}
    if not total:
        return out
    for tol, label in BUCKETS:
        out["buckets"][label] = sum(1 for d in diffs if abs(d) <= tol)
    out["bias"] = round(sum(diffs) / total, 2)      # 平均どれだけ後ろ倒しに外すか
    out["abs_mean"] = round(sum(abs(d) for d in diffs) / total, 2)
    return out


def report(title: str, r: dict) -> None:
    log(f"■ {title}: 検証 {r['n']} 件（履歴不足でスキップ {r['skipped']} 件）")
    if not r["n"]:
        return
    for _tol, label in BUCKETS:
        c = r["buckets"][label]
        log(f"    {label:>5} 以内: {c:6d} / {r['n']}  = {c * 100 / r['n']:5.1f}%")
    log(f"    平均ズレ {r['abs_mean']}日（符号つき平均 {r['bias']:+}日 ＝ +は予測が後ろ倒し）")


# ----------------------------------------------------------------------
# データ読み込み
# ----------------------------------------------------------------------
def load_jp_periods(hist_dir: Path) -> dict[str, dict[str, str]]:
    """J-Quants の決算短信履歴（fins_summary/*.json）から
    {銘柄コード: {期末: 最初の実発表日}} を作る。訂正開示は無視する。"""
    files = sorted(hist_dir.glob("*.json"))
    if not files:
        log(f"  開示履歴が見つからない: {hist_dir}")
        return {}
    log(f"  開示履歴を走査: {len(files)} ファイル")
    # {コード: {期末: 最初の開示日}}
    per: dict[str, dict[str, str]] = defaultdict(dict)
    for i, f in enumerate(files):
        try:
            rows = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in rows:
            if "FinancialStatements" not in (r.get("DocType") or ""):
                continue
            code, disc, end = r.get("Code"), r.get("DiscDate"), r.get("CurPerEn")
            if not (code and disc and end):
                continue
            if end not in per[code] or disc < per[code][end]:
                per[code][end] = disc
        if (i + 1) % 500 == 0:
            log(f"    {i + 1}/{len(files)}")
    return dict(per)


def load_jp_series(hist_dir: Path) -> dict[str, list[str]]:
    """{銘柄コード: 実発表日リスト}（evaluate 用・期末は捨てる）。"""
    return {code: sorted(m.values()) for code, m in load_jp_periods(hist_dir).items()}


# ----------------------------------------------------------------------
# 日本株「本番と同じ経路」の検証
#   evaluate() は10年ぶんの実発表日をぜんぶ候補にするが、本番の日本株は
#   jq_earnings_jp.json（jquants-bulk が事前生成）の候補日リストしか見ない。
#   そちらは「次の4四半期ぶんを1四半期1本ずつ」に絞ってあるので、素の履歴を
#   使う evaluate() とは候補の数がまるで違う。本番の実力はこちらで測る。
# ----------------------------------------------------------------------
QUARTERS_AHEAD = 4          # build_earnings_estimates.py と同じ
CYCLE_DAYS = 364


def _month_end(y: int, m: int) -> date:
    if m == 12:
        return date(y, 12, 31)
    return date(y, m + 1, 1) - timedelta(days=1)


def _add_months_end(d: date, months: int) -> date:
    """四半期末に months ヶ月足した月の月末（build_earnings_estimates.py と同じ）。"""
    m = d.month + months
    y = d.year + (m - 1) // 12
    return _month_end(y, (m - 1) % 12 + 1)


def evaluate_file_rule(periods: dict[str, dict[str, str]], min_history: int = 4,
                       rule: str = "anchored") -> dict:
    """本番の日本株と同じ手順で予測を作って当たり具合を返す。

    ある実発表日 D について、D より前に開示済みのぶんだけで
    jq_earnings_jp.json を作り直し（＝候補は1四半期1本）、
    そこから fetch_earnings.pick_next_date で1つ選ぶ。
    """
    diffs: list[int] = []
    n_skip = 0
    for _code, per in periods.items():
        # (期末, 開示日) を開示日の古い順に並べる
        rows = sorted(per.items(), key=lambda kv: kv[1])
        for i, (_end, actual_s) in enumerate(rows):
            known = dict(rows[:i])                 # ← その時点で開示済みのぶんだけ
            if len(known) < min_history:
                n_skip += 1
                continue
            last_end = date.fromisoformat(max(known))
            last_disc = date.fromisoformat(max(known.values()))
            asof = last_disc + timedelta(days=1)   # 決算通過直後に次を出す運用と同じ
            cands: list[date] = []
            for q in range(1, QUARTERS_AHEAD + 1):
                prev_end = _add_months_end(_add_months_end(last_end, 3 * q), -12).isoformat()
                base = known.get(prev_end)
                if base:
                    cands.append(date.fromisoformat(base) + timedelta(days=CYCLE_DAYS))
            pred = next_pick(cands, asof,
                             anchor=None if rule == "nearest" else last_disc, rule=rule)
            if pred is None:
                n_skip += 1
                continue
            diffs.append((pred - date.fromisoformat(actual_s)).days)
    total = len(diffs)
    out = {"n": total, "skipped": n_skip, "buckets": {}, "bias": None}
    if not total:
        return out
    for tol, label in BUCKETS:
        out["buckets"][label] = sum(1 for d in diffs if abs(d) <= tol)
    out["bias"] = round(sum(diffs) / total, 2)
    out["abs_mean"] = round(sum(abs(d) for d in diffs) / total, 2)
    return out


def load_us_series(symbols: list[str], samples: int) -> dict[str, list[str]]:
    """SEC EDGAR の 8-K(Item 2.02) 実績から {ティッカー: 実発表日リスト} を作る。"""
    import us_earnings_time as ue
    ue.SAMPLES = samples          # 検証用に履歴を長めに取る（本番は直近8件）
    m = ue.us_earnings_time_map(symbols)
    return {sym: sorted(v.get("dates") or []) for sym, v in m.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description="決算日予測の精度をアウトオブサンプルで測る")
    ap.add_argument("--jp", action="store_true", help="日本株（J-Quants開示履歴）を検証")
    ap.add_argument("--us", action="store_true", help="米国株（SEC EDGAR実績）を検証")
    ap.add_argument("--jq-history", default=str(ROOT.parent / "jquants-bulk" / "data" / "fins_summary"),
                    help="J-Quants の fins_summary フォルダ")
    ap.add_argument("--symbols", default="AAPL,MSFT,NVDA,KO,PG,JNJ,VZ,CVX,HD,MCD,MA,MCO,LMT,NOC,YUM,DG",
                    help="米国株の検証銘柄（カンマ区切り）")
    ap.add_argument("--samples", type=int, default=40, help="米国株で遡る 8-K の件数")
    ap.add_argument("--limit-codes", type=int, default=0,
                    help="日本株の検証銘柄数の上限（0＝全部）")
    args = ap.parse_args()
    if not (args.jp or args.us):
        args.jp = args.us = True
    rc = 1
    if args.jp:
        log("日本株（予測ルール: 昨年同四半期の実発表日 +364日）")
        periods = load_jp_periods(Path(args.jq_history))
        if args.limit_codes:
            periods = dict(sorted(periods.items())[:args.limit_codes])
        if periods:
            series = {code: sorted(m.values()) for code, m in periods.items()}
            # (a) 素の履歴を全部候補にした場合（ルールそのものの比較）
            for rule in ("nearest", "anchored"):
                report(f"日本株 全履歴を候補 ルール={rule}", evaluate(series, rule=rule))
            # (b) 本番と同じ経路（jq_earnings_jp.json＝1四半期1本の候補）
            for rule in ("nearest", "anchored"):
                report(f"日本株 本番と同じ候補 ルール={rule}",
                       evaluate_file_rule(periods, rule=rule))
            rc = 0
    if args.us:
        log("米国株（予測ルール: 同上。実績は SEC 8-K Item 2.02）")
        series = load_us_series([s.strip() for s in args.symbols.split(",") if s.strip()],
                                args.samples)
        if series:
            for rule in ("nearest", "anchored"):
                report(f"米国株 予測ルール={rule}", evaluate(series, rule=rule))
            rc = 0
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
