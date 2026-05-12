"""
data/investor_events.json に日本メジャーSQ（第2金曜日）を追加し、
既存の米国クアドラプル・ウィッチングのタイトルに「米」プレフィックスを付ける。

日本メジャーSQ：
- 3月/6月/9月/12月の第2金曜日
- 寄り付き 09:00 JST で特別清算指数を算出
- 株価指数先物・指数オプションの最終決済価格
"""
from __future__ import annotations

import json
import sys
from calendar import Calendar
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "investor_events.json"


def second_friday(year: int, month: int) -> datetime:
    """指定年月の第2金曜日を返す。"""
    cal = Calendar()
    fridays = [d for d in cal.itermonthdates(year, month)
               if d.month == month and d.weekday() == 4]  # Friday=4
    return datetime(fridays[1].year, fridays[1].month, fridays[1].day)


def build_jp_major_sq_events(year: int) -> list[dict]:
    """日本メジャーSQ 4件（3/6/9/12月の第2金曜日 09:00 JST）。"""
    events: list[dict] = []
    for month in (3, 6, 9, 12):
        d = second_friday(year, month)
        datetime_local = f"{d.year:04d}-{d.month:02d}-{d.day:02d}T09:00:00+09:00"
        # JST = UTC+9 → UTC = JST - 9h
        utc_dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
        datetime_utc = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        events.append({
            "id": f"jp_majorsq_{d.year:04d}-{d.month:02d}-{d.day:02d}",
            "title": f"日メジャーSQ ({d.month}月)",
            "category": "MARKET",
            "country": "JP",
            "datetime_utc": datetime_utc,
            "datetime_local": datetime_local,
            "timezone": "Asia/Tokyo",
            "importance": 2,
            "is_estimated": False,
            "description": (
                "日経225先物・TOPIX先物・指数オプションの特別清算指数（SQ）算出日"
                "（第2金曜日 09:00 寄り付き）。算出値は寄り付きの板で決まる。"
            ),
            "source_url": None,
        })
    return events


def main() -> int:
    with SRC.open(encoding="utf-8") as f:
        data = json.load(f)

    covered_years = sorted({int(e["datetime_utc"][:4]) for e in data["events"]})
    print(f"既存 events 件数: {len(data['events'])}, 対象年: {covered_years}", file=sys.stderr)

    # 1) 既存の米国 quadwitch のタイトルに「米」プレフィックスを付加
    renamed = 0
    for ev in data["events"]:
        if ev["id"].startswith("us_quadwitch_") and not ev["title"].startswith("米"):
            ev["title"] = "米" + ev["title"]
            renamed += 1
    print(f"米国 quadwitch タイトル更新: {renamed} 件", file=sys.stderr)

    # 2) 日本メジャーSQ を追加
    existing_ids = {e["id"] for e in data["events"]}
    added: list[dict] = []
    for year in covered_years:
        for ev in build_jp_major_sq_events(year):
            if ev["id"] in existing_ids:
                continue
            added.append(ev)

    if added or renamed:
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
    else:
        print("変更なし", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
