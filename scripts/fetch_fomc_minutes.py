"""
FOMC 会合日から議事要旨（FOMC Minutes）の公表日を逆算し、
tmp/fetch_fomc_minutes_out.json に出力する。

議事要旨は「政策決定日（声明発表日）の3週間後 14:00 ET」に公表される
（FRB公式ルール: "released three weeks after the date of the policy decision"）。
fetch_beige.py（会合14日前）と同じ方式で、FOMC 日程を +21日 ずらすだけで確定できる。

入力（どちらか・上を優先）:
- tmp/fetch_fomc_out.json   … fetch_fomc.py の出力（FRB公式・確定）
- tmp/build_events_out.json … 上が無ければ build_events.py 出力の category=="FOMC"

出力:
- 元の FOMC が is_estimated=False（確定）なら議事要旨も False を継承
- target_years（build_events_out.json の covered_years）でフィルタ
- パース失敗・FOMC不在時は何も書かない（pipeline は他カテゴリで継続）
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import write_tmp, load_tmp, log  # noqa: E402


MINUTES_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

# タイトル表示用の日本語月ラベル
MONTH_JA = {1: "1月", 2: "2月", 3: "3月", 4: "4月", 5: "5月", 6: "6月",
            7: "7月", 8: "8月", 9: "9月", 10: "10月", 11: "11月", 12: "12月"}


def _et_offset_hours(year: int, month: int, day: int) -> int:
    """その日付の米東部時間オフセット（時間数）を返す。EST=5、EDT=4。"""
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        local = datetime(year, month, day, 14, 0, 0, tzinfo=et)
        off = local.utcoffset()
        if off is not None:
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


def minutes_from_fomc(fomc: dict) -> dict | None:
    """1つの FOMC イベントから、21日後の議事要旨公表イベントを作る。"""
    local_str = fomc.get("datetime_local")
    if not local_str:
        return None
    try:
        fomc_local = datetime.fromisoformat(local_str)
    except ValueError:
        return None

    # FOMC 声明日（水）の21日後 → 議事要旨公表日（同じ水曜）
    mn_day = (fomc_local + timedelta(days=21)).date()
    off_h = _et_offset_hours(mn_day.year, mn_day.month, mn_day.day)
    offset = f"-{off_h:02d}:00"
    datetime_local = f"{mn_day.year:04d}-{mn_day.month:02d}-{mn_day.day:02d}T14:00:00{offset}"
    utc_hour = 14 + off_h  # 14 ET + 5(EST) または +4(EDT) → 19 or 18 UTC
    datetime_utc = f"{mn_day.year:04d}-{mn_day.month:02d}-{mn_day.day:02d}T{utc_hour:02d}:00:00Z"

    fomc_month = fomc_local.month
    ev_id = f"us_fomc_minutes_{mn_day.year:04d}-{mn_day.month:02d}-{mn_day.day:02d}"
    title = f"FOMC議事要旨 ({MONTH_JA[fomc_month]}会合分)"
    return {
        "id": ev_id,
        "title": title,
        "category": "FOMC",
        "country": "US",
        "datetime_utc": datetime_utc,
        "datetime_local": datetime_local,
        "timezone": "America/New_York",
        "importance": 2,
        "is_estimated": bool(fomc.get("is_estimated", False)),
        "description": "FOMC議事要旨（Minutes）。会合の3週間後(水)14:00 ET公表。"
                       "利上げ/利下げ議論の内訳が分かり、タカ派/ハト派サプライズで相場が動くことがある。",
        "source_url": MINUTES_URL,
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
        evs = [e for e in data.get("events", [])
               if e.get("category") == "FOMC"
               and not str(e.get("id", "")).startswith("us_fomc_minutes_")]
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
        log("fetch_fomc_minutes: FOMC イベントが無いため生成スキップ（後続は他カテゴリで継続）")
        return 0

    target_years = load_target_years()
    by_id: dict[str, dict] = {}
    for f in fomc_events:
        mn = minutes_from_fomc(f)
        if mn is None:
            continue
        prefix = len("us_fomc_minutes_")
        year = int(mn["id"][prefix:prefix + 4])
        if year in target_years:
            by_id[mn["id"]] = mn

    minutes = sorted(by_id.values(), key=lambda e: e["datetime_utc"])
    if not minutes:
        log(f"fetch_fomc_minutes: target_years={sorted(target_years)} 内に議事要旨が無くスキップ")
        return 0

    path = write_tmp("fetch_fomc_minutes_out", {
        "events": minutes,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    log(f"fetch_fomc_minutes: {len(minutes)} 件の議事要旨を {path} に書き出し")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
