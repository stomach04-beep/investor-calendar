"""
data/investor_events.json 縺ｫ譌･譛ｬ繝｡繧ｸ繝｣繝ｼSQ・育ｬｬ2驥第屆譌･・峨ｒ霑ｽ蜉縺励・
譌｢蟄倥・邀ｳ蝗ｽ繧ｯ繧｢繝峨Λ繝励Ν繝ｻ繧ｦ繧｣繝・メ繝ｳ繧ｰ縺ｮ繧ｿ繧､繝医Ν縺ｫ縲檎ｱｳ縲阪・繝ｬ繝輔ぅ繝・け繧ｹ繧剃ｻ倥￠繧九・

譌･譛ｬ繝｡繧ｸ繝｣繝ｼSQ・・
- 3譛・6譛・9譛・12譛医・隨ｬ2驥第屆譌･
- 蟇・ｊ莉倥″ 09:00 JST 縺ｧ迚ｹ蛻･貂・ｮ玲欠謨ｰ繧堤ｮ怜・
- 譬ｪ萓｡謖・焚蜈育黄繝ｻ謖・焚繧ｪ繝励す繝ｧ繝ｳ縺ｮ譛邨よｱｺ貂井ｾ｡譬ｼ
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
    """謖・ｮ壼ｹｴ譛医・隨ｬ2驥第屆譌･繧定ｿ斐☆縲・""
    cal = Calendar()
    fridays = [d for d in cal.itermonthdates(year, month)
               if d.month == month and d.weekday() == 4]  # Friday=4
    return datetime(fridays[1].year, fridays[1].month, fridays[1].day)


def build_jp_major_sq_events(year: int) -> list[dict]:
    """譌･譛ｬ繝｡繧ｸ繝｣繝ｼSQ 4莉ｶ・・/6/9/12譛医・隨ｬ2驥第屆譌･ 09:00 JST・峨・""
    events: list[dict] = []
    for month in (3, 6, 9, 12):
        d = second_friday(year, month)
        datetime_local = f"{d.year:04d}-{d.month:02d}-{d.day:02d}T09:00:00+09:00"
        # JST = UTC+9 竊・UTC = JST - 9h
        utc_dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)  # 09:00 JST = 00:00 UTC
        datetime_utc = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        events.append({
            "id": f"jp_majorsq_{d.year:04d}-{d.month:02d}-{d.day:02d}",
            "title": f"譌･繝｡繧ｸ繝｣繝ｼSQ ({d.month}譛・",
            "category": "MARKET",
            "country": "JP",
            "datetime_utc": datetime_utc,
            "datetime_local": datetime_local,
            "timezone": "Asia/Tokyo",
            "importance": 2,
            "is_estimated": False,
            "description": (
                "譌･邨・25蜈育黄繝ｻTOPIX蜈育黄繝ｻ謖・焚繧ｪ繝励す繝ｧ繝ｳ縺ｮ迚ｹ蛻･貂・ｮ玲欠謨ｰ・・Q・臥ｮ怜・譌･"
                "・育ｬｬ2驥第屆譌･ 09:00 蟇・ｊ莉倥″・峨らｮ怜・蛟､縺ｯ蟇・ｊ莉倥″縺ｮ譚ｿ縺ｧ豎ｺ縺ｾ繧九・
            ),
            "source_url": None,
        })
    return events


def main() -> int:
    with SRC.open(encoding="utf-8") as f:
        data = json.load(f)

    covered_years = sorted({int(e["datetime_utc"][:4]) for e in data["events"]})
    print(f"譌｢蟄・events 莉ｶ謨ｰ: {len(data['events'])}, 蟇ｾ雎｡蟷ｴ: {covered_years}", file=sys.stderr)

    # 1) 譌｢蟄倥・邀ｳ蝗ｽ quadwitch 縺ｮ繧ｿ繧､繝医Ν縺ｫ縲檎ｱｳ縲阪・繝ｬ繝輔ぅ繝・け繧ｹ繧剃ｻ伜刈・・邀ｳ繧ｯ繧｢繝峨Λ繝励Ν繝ｻ繧ｦ繧｣繝・メ繝ｳ繧ｰ (..)"・・
    renamed = 0
    for ev in data["events"]:
        if ev["id"].startswith("us_quadwitch_") and not ev["title"].startswith("邀ｳ"):
            ev["title"] = "邀ｳ" + ev["title"]
            renamed += 1
    print(f"邀ｳ蝗ｽ quadwitch 繧ｿ繧､繝医Ν譖ｴ譁ｰ: {renamed} 莉ｶ", file=sys.stderr)

    # 2) 譌･譛ｬ繝｡繧ｸ繝｣繝ｼSQ 繧定ｿｽ蜉
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
        print(f"霑ｽ蜉 {len(added)} 莉ｶ:", file=sys.stderr)
        for ev in added:
            print(f"  {ev['id']} {ev['datetime_local']} {ev['title']}", file=sys.stderr)
        print(f"events 蜷郁ｨ・ {len(data['events'])} 莉ｶ", file=sys.stderr)
    else:
        print("螟画峩縺ｪ縺・, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
