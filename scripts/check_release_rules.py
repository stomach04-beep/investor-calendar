"""
公開する investor_events.json を規則表（release_rules）と突き合わせて、ずれを警告する。

見ること（今日〜先 200 日の予定だけ。過去の記録で毎日鳴らさない）:
  1. 公表時刻が規則と違う（例: 日本GDP が 8:50 JST でない＝タイムゾーンの二重がけ）
  2. 土日・休場日・祝日に乗っている（ルール計算や手入力が祝日を踏んだ）
  3. 規則の無い単発の予定が現地の深夜 1:00〜5:59 にある（終日の予定のタイムゾーン違い）

警告は ::warning:: で出す（health-watchdog が Actions の注釈を読んで LINE へ届ける）。
ジョブは止めない（終了コード 0）。--strict のときだけ、ずれがあれば終了コード 1。

使い方:
  python scripts/check_release_rules.py              # data/investor_events.json を検査
  python scripts/check_release_rules.py --file X.json --strict
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_rules import is_open, local_datetime, rule_for  # noqa: E402

DEFAULT_FILE = Path(__file__).resolve().parents[1] / "data" / "investor_events.json"
LOOKAHEAD_DAYS = 200


# 規則の無い単発の予定でも、現地の 1:00〜5:59 は発表・開催の時刻としてまず無い。
# 「終日」の予定を UTC で手書きして時差がずれた形（春闘 1:00・税制改正大綱 1:00 など）を拾う。
ODD_HOURS = range(1, 6)


def odd_hour_problem(ev: dict, today: date, until: date) -> str | None:
    """規則の無い予定が、そのタイムゾーンの深夜（1:00〜5:59）に置かれていれば説明を返す。"""
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(ev.get("timezone") or "Asia/Tokyo")
    except Exception:
        return None
    utc = datetime.fromisoformat(str(ev["datetime_utc"]).replace("Z", "+00:00"))
    local = utc.astimezone(zone)
    if not (today <= local.date() <= until) or local.hour not in ODD_HOURS:
        return None
    return (f"{ev.get('title', '')}（{ev.get('id')}）: 現地時刻 {local:%m/%d %H:%M}（{zone.key}）は"
            f"深夜で不自然（終日の予定のタイムゾーン違いの疑い）")


def find_problems(events: list[dict], today: date) -> list[str]:
    """規則に合わない予定を1件1行の説明にして返す。"""
    problems = []
    until = today + timedelta(days=LOOKAHEAD_DAYS)
    for ev in events:
        rule = rule_for(ev)
        if not ev.get("datetime_utc"):
            continue
        if rule is None:
            problem = odd_hour_problem(ev, today, until)
            if problem:
                problems.append(problem)
            continue
        local = local_datetime(ev, rule)
        if not (today <= local.date() <= until):
            continue
        name = f"{ev.get('title', '')}（{ev.get('id')}）"
        if (local.hour, local.minute) != (rule.hour, rule.minute):
            problems.append(
                f"{name}: 時刻 {local:%m/%d %H:%M} が規則（{rule.label} {rule.hour}:{rule.minute:02d} {rule.tz}）と違う")
        if rule.open_day and not is_open(rule, local.date()):
            problems.append(f"{name}: {local:%m/%d(%a)} は土日・休場日")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="公開予定を公表時刻・営業日の規則と突き合わせる")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--strict", action="store_true", help="ずれがあれば終了コード 1")
    args = parser.parse_args()

    data = json.loads(args.file.read_text(encoding="utf-8"))
    events = data.get("events", []) if isinstance(data, dict) else data
    today = datetime.now(timezone.utc).date()
    problems = find_problems(events, today)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = [f"## 公表時刻・営業日の検査（{len(problems)} 件）"] + [f"- {p}" for p in problems]
    for p in problems:
        print(f"::warning::日程の規則ずれ {p}")
    if not problems:
        print("日程の規則ずれ 0 件")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return 1 if (args.strict and problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
