"""
FOMC 会合日からベージュブック（地区連銀経済報告）の公表日を逆算し、
tmp/fetch_beige_out.json に出力する。

ベージュブックは FOMC 会合の「2週間前の水曜 14:00 ET」に年8回公表される。
FOMC 声明発表日（2日目＝水曜）のちょうど14日前に当たるため、
fetch_fomc.py が取得した FOMC 日程を14日ずらすだけで確定できる。
（2026-04-15 公表、2026-06-03 公表の実績で「FOMC声明日 -14日」が裏取り済み）

入力（どちらか・上を優先）:
- tmp/fetch_fomc_out.json   … fetch_fomc.py の出力（FRB公式・確定）
- tmp/build_events_out.json … 上が無ければ build_events.py 出力の category=="FOMC"

出力:
- 元の FOMC が is_estimated=False（確定）ならベージュブックも False を継承
- target_years（build_events_out.json の covered_years）でフィルタ
- パース失敗・FOMC不在時は何も書かない（pipeline は他カテゴリで継続）
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import write_tmp, load_tmp, log  # noqa: E402


BEIGE_URL = "https://www.federalreserve.gov/monetarypolicy/publications/beige-book-default.htm"

# タイトル表示用の日本語月ラベル
MONTH_JA = {1: "1月", 2: "2月", 3: "3月", 4: "4月", 5: "5月", 6: "6月",
            7: "7月", 8: "8月", 9: "9月", 10: "10月", 11: "11月", 12: "12月"}


def _et_offset_hours(year: int, month: int, day: int) -> int:
    """
    その日付の米東部時間オフセット（時間数）を返す。EST=5、EDT=4。

    zoneinfo が使えればそれで正確に判定し、使えなければ米国 DST 規則
    （3月第2日曜〜11月第1日曜が夏時間）で粗く判定するフォールバック。
    """
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        local = datetime(year, month, day, 14, 0, 0, tzinfo=et)
        off = local.utcoffset()
        if off is not None:
            # utcoffset は -5:00 のような負値。時間数に直して符号反転
            return int(round(-off.total_seconds() / 3600))
    except Exception:
        pass

    # --- フォールバック: 米国 DST 規則を手計算 ---
    def nth_sunday(y: int, m: int, n: int) -> int:
        first_weekday = datetime(y, m, 1).weekday()  # Mon=0..Sun=6
        first_sunday = 1 + (6 - first_weekday) % 7
        return first_sunday + (n - 1) * 7

    dst_start = datetime(year, 3, nth_sunday(year, 3, 2))   # 3月第2日曜
    dst_end = datetime(year, 11, nth_sunday(year, 11, 1))   # 11月第1日曜
    today = datetime(year, month, day)
    return 4 if dst_start <= today < dst_end else 5


def beige_from_fomc(fomc: dict) -> dict | None:
    """1つの FOMC イベントから、14日前のベージュブック公表イベントを作る。"""
    local_str = fomc.get("datetime_local")
    if not local_str:
        return None
    try:
        fomc_local = datetime.fromisoformat(local_str)
    except ValueError:
        return None

    # FOMC 声明日（水）の14日前 → ベージュブック公表日（同じ水曜）
    bb_day = (fomc_local - timedelta(days=14)).date()
    off_h = _et_offset_hours(bb_day.year, bb_day.month, bb_day.day)
    offset = f"-{off_h:02d}:00"
    datetime_local = f"{bb_day.year:04d}-{bb_day.month:02d}-{bb_day.day:02d}T14:00:00{offset}"
    utc_hour = 14 + off_h  # 14 ET + 5(EST) または +4(EDT) → 19 or 18 UTC
    datetime_utc = f"{bb_day.year:04d}-{bb_day.month:02d}-{bb_day.day:02d}T{utc_hour:02d}:00:00Z"

    fomc_month = fomc_local.month
    ev_id = f"us_beige_{bb_day.year:04d}-{bb_day.month:02d}-{bb_day.day:02d}"
    title = f"ベージュブック ({MONTH_JA[fomc_month]}FOMC前)"
    return {
        "id": ev_id,
        "title": title,
        "category": "BEIGE",
        "country": "US",
        "datetime_utc": datetime_utc,
        "datetime_local": datetime_local,
        "timezone": "America/New_York",
        "importance": 2,
        "is_estimated": bool(fomc.get("is_estimated", False)),
        "description": "地区連銀経済報告（Beige Book）。FOMC会合の2週間前(水)14:00 ET公表。全12地区の景況感まとめで、利上げ/利下げ判断の地ならし材料。",
        "source_url": BEIGE_URL,
    }


def load_fomc_events() -> list[dict]:
    """FOMC 一覧を取得。fetch_fomc_out を最優先、無ければ build_events_out の FOMC。"""
    try:
        data = load_tmp("fetch_fomc_out")
        evs = data.get("events", []) if isinstance(data, dict) else []
        if evs:
            log(f"  fetch_fomc_out.json から FOMC {len(evs)} 件を読み込み（公式確定）")
            return evs
    except FileNotFoundError:
        pass
    try:
        data = load_tmp("build_events_out")
        evs = [e for e in data.get("events", []) if e.get("category") == "FOMC"]
        if evs:
            log(f"  build_events_out.json から FOMC {len(evs)} 件を読み込み（推定含む）")
        return evs
    except FileNotFoundError:
        return []


def load_target_years() -> set[int]:
    """build_events_out.json の covered_years。無ければ今年と来年。"""
    try:
        data = load_tmp("build_events_out")
        years = set(int(y) for y in data.get("covered_years", []))
        if years:
            return years
    except Exception:
        pass
    now_year = datetime.now(timezone.utc).year
    return {now_year, now_year + 1}


def main() -> int:
    fomc_events = load_fomc_events()
    if not fomc_events:
        log("fetch_beige: FOMC イベントが無いため生成スキップ（後続は他カテゴリで継続）")
        return 0

    target_years = load_target_years()
    by_id: dict[str, dict] = {}
    for f in fomc_events:
        bb = beige_from_fomc(f)
        if bb is None:
            continue
        prefix = len("us_beige_")
        year = int(bb["id"][prefix:prefix + 4])
        if year in target_years:
            by_id[bb["id"]] = bb

    beige = sorted(by_id.values(), key=lambda e: e["datetime_utc"])
    if not beige:
        log(f"fetch_beige: target_years={sorted(target_years)} 内にベージュブックが無くスキップ")
        return 0

    path = write_tmp("fetch_beige_out", {
        "events": beige,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    log(f"fetch_beige: {len(beige)} 件のベージュブックを {path} に書き出し")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
