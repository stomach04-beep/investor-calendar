"""
data/investor_events.json に米国クアドラプル・ウィッチング（SQ）を追加するワンショット。

実行後はリポから削除可（一度走らせれば seed に4件入り、以降は build_events の
52週シフトで翌年以降も自動生成される）。

クアドラプル・ウィッチング：
- 3月/6月/9月/12月の第3金曜日
- 米国株価指数先物・指数オプション・個別株先物・個別株オプションが同時満期
- 市場のボラティリティが上がる
"""
from __future__ import annotations

import json
import sys
from calendar import Calendar
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "investor_events.json"


def third_friday(year: int, month: int) -> datetime:
    """指定年月の第3金曜日を返す。"""
    cal = Calendar()
    fridays = [d for d in cal.itermonthdates(year, month)
               if d.month == month and d.weekday() == 4]  # Monday=0, Friday=4
    return datetime(fridays[2].year, fridays[2].month, fridays[2].day)


def is_us_dst(d: datetime) -> bool:
    """米国の DST 期間判定（簡易、2007年以降のルール）：3月第2日曜〜11月第1日曜。"""
    cal = Calendar()
    march_sundays = [day for day in cal.itermonthdates(d.year, 3)
                     if day.month == 3 and day.weekday() == 6]
    dst_start = datetime(d.year, 3, march_sundays[1].day)
    nov_sundays = [day for day in cal.itermonthdates(d.year, 11)
                   if day.month == 11 and day.weekday() == 6]
    dst_end = datetime(d.year, 11, nov_sundays[0].day)
    return dst_start <= d < dst_end


def build_quad_witching_events(year: int) -> list[dict]:
    events: list[dict] = []
    for month in (3, 6, 9, 12):
        d = third_friday(year, month)
        # 16:00 ET 取引終了。DST 期間中は EDT(-04:00) で UTC=20:00、外は EST(-05:00) で UTC=21:00
        if is_us_dst(d):
            offset = "-04:00"
            utc_hour = 20
        else:
            offset = "-05:00"
            utc_hour = 21
        datetime_local = f"{d.year:04d}-{d.month:02d}-{d.day:02d}T16:00:00{offset}"
        datetime_utc = f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{utc_hour:02d}:00:00Z"
        ev = {
            "id": f"us_quadwitch_{d.year:04d}-{d.month:02d}-{d.day:02d}",
            "title": f"クアドラプル・ウィッチング ({d.month}月SQ)",
            "category": "MARKET",
            "country": "US",
            "datetime_utc": datetime_utc,
            "datetime_local": datetime_local,
            "timezone": "America/New_York",
            "importance": 2,
            "is_estimated": False,
            "description": (
                "米国株価指数先物・指数オプション・個別株先物・個別株オプションが同時満期"
                "（第3金曜日）。引け前後のボラティリティ上昇と出来高急増に注意。"
            ),
            "source_url": None,
        }
        events.append(ev)
    return events


def main() -> int:
    with SRC.open(encoding="utf-8") as f:
        data = json.load(f)

    covered_years = sorted({int(e["datetime_utc"][:4]) for e in data["events"]})
    print(f"既存 events 件数: {len(data['events'])}, 対象年: {covered_years}", file=sys.stderr)

    # 既存ID集合（重複防止）
    existing_ids = {e["id"] for e in data["events"]}

    added: list[dict] = []
    for year in covered_years:
        for ev in build_quad_witching_events(year):
            if ev["id"] in existing_ids:
                print(f"  既存スキップ: {ev['id']}", file=sys.stderr)
                continue
            added.append(ev)

    if not added:
        print("追加対象なし（既に全件追加済み）", file=sys.stderr)
        return 0

    data["events"].extend(added)
    data["events"].sort(key=lambda e: e["datetime_utc"])
    data["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with SRC.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"追加 {len(added)} 件:", file=sys.stderr)
    for ev in added:
        print(f"  {ev['id']} {ev['datetime_local']} {ev['title']}", file=sys.stderr)
    print(f"events 合計: {len(data['events'])} 件", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
