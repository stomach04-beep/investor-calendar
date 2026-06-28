"""
data/investor_events.json をベースに、Notion に投入する全カテゴリの events リストを作る。

現在の挙動：
- data/investor_events.json をシード（テンプレ）として読む
- --shift-to-year YYYY を指定すると、その年用に 52週シフトしたコピーを追加生成
- 出力: tmp/build_events_out.json （events 配列を含む辞書）

後続スクリプト：
- fetch_fomc.py / fetch_boj.py が同じ tmp/ にカテゴリ別の上書き候補を出力
- notion_upsert.py がこれら全部をマージして Notion に upsert
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# scripts/ を import path に入れる
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_canonical_events, write_tmp, log  # noqa: E402


def shift_event_to_year(event: dict, baseline_year: int, target_year: int) -> dict | None:
    """
    1イベントを baseline_year から target_year に 52週シフトする。
    曜日パターンを維持する目的（米雇用統計 = 第1金曜日 等）。

    タイトル・id 内の年表記も置換する。シフトしたイベントは is_estimated=True にする。
    """
    delta_years = target_year - baseline_year
    if delta_years == 0:
        return None  # シフト不要
    delta = timedelta(weeks=52 * delta_years)

    # datetime_utc は "YYYY-MM-DDTHH:MM:SSZ" 形式
    utc_dt = datetime.fromisoformat(event["datetime_utc"].replace("Z", "+00:00"))
    new_utc = utc_dt + delta
    # datetime_local は "YYYY-MM-DDTHH:MM:SS±HH:MM" 形式
    local_dt = datetime.fromisoformat(event["datetime_local"])
    new_local = local_dt + delta

    # id の年表記置換（最初に現れる YYYY のみ）
    new_id = re.sub(rf"\b{baseline_year}\b", str(target_year), event["id"], count=1)

    # title の年表記置換：baseline-1, baseline の両方を target-1, target に置換
    # 例: "2025年12月分" → "2026年12月分"、"(1月会合)" は触らない
    new_title = event["title"]
    new_title = re.sub(rf"\b{baseline_year - 1}\b", str(target_year - 1), new_title)
    new_title = re.sub(rf"\b{baseline_year}\b", str(target_year), new_title)

    out = dict(event)
    out["id"] = new_id
    out["title"] = new_title
    out["datetime_utc"] = new_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    # ローカル日時の isoformat はオフセット込みで返るので OK
    out["datetime_local"] = new_local.isoformat()
    out["is_estimated"] = True  # シフトしたイベントは必ず推定扱い
    return out


def build(target_years: list[int]) -> dict:
    """シード + 必要に応じて年シフトを行い、events をまとめて返す。"""
    seed = load_canonical_events()
    # 保有株決算(hold_earnings_*)・ウォッチ銘柄決算(watch_earnings_*)は fetch_earnings.py の
    # 専管。notion_to_json が投資家カレンダーDB全体を investor_events.json へ書き戻すため、
    # 前回生成した決算イベントがシードに紛れ込む。build_events では除外し、毎回
    # fetch_earnings_out だけが供給元になるようにする（archive した銘柄が build 経由で復活するのを防ぐ）。
    # アノマリー(anomaly_*)も fetch_anomalies.py の専管。暦ベースの固定日付なので
    # 52週シフトすると日付がズレる（節分=2/3 等が崩れる）。決算と同じくシードから除外し、
    # 毎回 fetch_anomalies_out だけが供給元になるようにする。
    seed_events = [
        e for e in seed.get("events", [])
        if not str(e.get("id", "")).startswith(
            ("hold_earnings_", "watch_earnings_", "anomaly_")
        )
    ]
    covered = set(seed.get("covered_years", []))

    if not seed_events:
        raise RuntimeError("seed JSON に events がありません")

    # シードの中央年を baseline とする（複数年カバーしていれば最小年）
    baseline_year = min(covered) if covered else 2026

    new_events = list(seed_events)
    new_covered = set(covered)
    skipped: list[int] = []
    for year in target_years:
        if year in new_covered:
            skipped.append(year)
            continue
        log(f"  generating events for {year} by shifting from {baseline_year}")
        for ev in seed_events:
            # baseline 年のイベントだけシフト対象（重複防止）
            if str(baseline_year) not in ev["id"]:
                continue
            shifted = shift_event_to_year(ev, baseline_year, year)
            if shifted is not None:
                new_events.append(shifted)
        new_covered.add(year)

    if skipped:
        log(f"  既にカバー済みの年はスキップ: {skipped}")

    # 重複 id をマージ（後勝ち）
    by_id: dict[str, dict] = {}
    for ev in new_events:
        by_id[ev["id"]] = ev
    deduped = sorted(by_id.values(), key=lambda e: e["datetime_utc"])

    out = dict(seed)
    out["events"] = deduped
    out["covered_years"] = sorted(new_covered)
    out["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="投資家カレンダーのイベント一覧を構築")
    parser.add_argument(
        "--shift-to-year",
        type=int,
        action="append",
        default=[],
        help="対象年（複数指定可、未カバーなら 52週シフトでコピー生成）",
    )
    args = parser.parse_args()

    out = build(args.shift_to_year)
    path = write_tmp("build_events_out", out)
    log(f"build_events: 全 {len(out['events'])} 件、対象年={out['covered_years']} を {path} に書き出し")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
