"""
EIA 週次石油在庫統計（Weekly Petroleum Status Report）を投資家カレンダーへ生成する。

米エネルギー情報局(EIA)が毎週公表する原油・ガソリン在庫の増減統計。
原油(WTI)が最も素直に反応する定例イベントで、CFD判断支援アプリ CfdWatch が
独自に持っていたカレンダーを本パイプラインへ統合したもの。

■ 発表ルール（EIA公式で裏取り済み）
  - 通常週: 毎週水曜 10:30 a.m. ET（サマリー・Tables 1-14 の公開時刻）
  - 祝日週: 「for some weeks that include holidays, releases are delayed by one day」
            繰り下げ先の日付・時刻は年ごとに違う（木曜12:00 / 木曜11:00 /
            クリスマス週は月曜17:00 など不規則）ため**ルールで推測せず公式表を読む**。
    公式表: https://www.eia.gov/petroleum/supply/weekly/schedule.php

■ 設計（推測日付を作らないための約束）
  - id = us_eia_{対象週の金曜}  … 発表日ではなく「対象週」で固定する。
    水→木に繰り下がっても id が変わらない（[[feedback_upsert_id_immutable]]）。
  - is_estimated
      通常水曜（ルール計算）      → True  … 後から公式表に祝日繰り下げが載ったら追従できる
      公式表に載っている繰り下げ週 → False … 一次情報なので確定（notion_upsert が保護）
    ※ 通常水曜を False にすると、EIA が後から繰り下げを公表したときに
      保護されて誤日付のまま凍結する。あえて True にしている。
  - 生成範囲は covered_years 全部ではなく**前後ローリング窓**（既定 -14日〜+120日）。
    公式の祝日表は約1年分しか載らず、翌年分まで作ると全部が根拠なしの推測になるため。
    毎朝再生成されるので窓は自然に前へ進む。

出力: tmp/fetch_eia_out.json （events 配列を含む辞書）
      → notion_upsert.py が build_events_out にマージして Notion に upsert

実行:
    python scripts/fetch_eia.py              # tmp/fetch_eia_out.json を生成
    python scripts/fetch_eia.py --self-test  # 公式取得＋生成結果を表示（書き込み無し）
    python scripts/fetch_eia.py --offline    # 公式へ接続せず真値表のみで生成（テスト用）
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import write_tmp, log, record_fetch_warning  # noqa: E402

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

USER_AGENT = "investor-calendar-bot/1.0 (+https://github.com/stomach04-beep/investor-calendar)"

# 公式スケジュール（祝日による繰り下げ一覧が載る）
EIA_SCHEDULE_URL = "https://www.eia.gov/petroleum/supply/weekly/schedule.php"
# イベントの参照先（レポート本体）
EIA_REPORT_URL = "https://www.eia.gov/petroleum/supply/weekly/"

# 通常週の公表時刻（ET）
NORMAL_HOUR, NORMAL_MINUTE = 10, 30

# ローリング窓（今日を基準に何日前から何日後まで作るか）
WINDOW_BACK_DAYS = 14
WINDOW_FORWARD_DAYS = 120

# ----------------------------------------------------------------------
# 祝日繰り下げの真値表（公式ページが取れなかったときのフォールバック）
#   key   : 本来の公表水曜日（＝対象週の金曜 + 5日）
#   value : (実際の公表日, 時, 分, 祝日名)
#   2026-07-26 に公式 schedule.php から転記。**年1回の更新が必要**
#   （切れたら record_fetch_warning → health-watchdog が LINE 通知する）
# ----------------------------------------------------------------------
EIA_HOLIDAY_TRUTH: dict[date, tuple[date, int, int, str]] = {
    date(2026, 1, 21): (date(2026, 1, 22), 12, 0, "Martin Luther King Jr. Day"),
    date(2026, 2, 18): (date(2026, 2, 19), 12, 0, "President's Day"),
    date(2026, 5, 27): (date(2026, 5, 28), 12, 0, "Memorial Day"),
    date(2026, 9, 9): (date(2026, 9, 10), 12, 0, "Labor Day"),
    date(2026, 10, 14): (date(2026, 10, 15), 12, 0, "Columbus Day"),
    date(2026, 11, 11): (date(2026, 11, 12), 12, 0, "Veterans Day"),
}
# 真値表がカバーしている最終日。窓がこれを追い越したら警告を出す
TRUTH_COVERED_UNTIL = date(2026, 12, 31)


# ----------------------------------------------------------------------
# 公式スケジュール表のパース
# ----------------------------------------------------------------------
def _strip_tags(html: str) -> str:
    """HTMLタグと連続空白を落として素のテキストにする。"""
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _parse_us_date(text: str) -> date | None:
    """"January 22, 2026" 形式を date にする。読めなければ None。"""
    try:
        return datetime.strptime(text.strip(), "%B %d, %Y").date()
    except ValueError:
        return None


def _parse_us_time(text: str) -> tuple[int, int] | None:
    """"12:00 p.m." / "11:00 a.m." 形式を (時, 分) の24時間表記にする。"""
    m = re.match(r"(\d{1,2}):(\d{2})\s*([ap])\.?m\.?", text.strip(), re.IGNORECASE)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if hour == 12:
        hour = 0
    if ampm == "p":
        hour += 12
    return hour, minute


def parse_schedule_html(html: str) -> dict[date, tuple[date, int, int, str]]:
    """公式スケジュールHTMLから「祝日で繰り下がる週」の一覧を抜き出す。

    表の列は [対象週の金曜, 実際の公表日, 曜日, 時刻, 祝日名]。
    戻り値は {本来の公表水曜: (実際の公表日, 時, 分, 祝日名)}。
    本来の公表水曜 = 対象週の金曜 + 5日（例: 1/16金 → 1/21水）。
    """
    result: dict[date, tuple[date, int, int, str]] = {}
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = [_strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)]
        if len(cells) < 5:
            continue
        week_ending = _parse_us_date(cells[0])
        release_date = _parse_us_date(cells[1])
        release_time = _parse_us_time(cells[3])
        if week_ending is None or release_date is None or release_time is None:
            continue  # ヘッダ行や注記行
        normal_wednesday = week_ending + timedelta(days=5)
        hour, minute = release_time
        result[normal_wednesday] = (release_date, hour, minute, cells[4])
    return result


def fetch_holiday_overrides(offline: bool = False) -> tuple[dict[date, tuple[date, int, int, str]], bool]:
    """公式から祝日繰り下げ表を取得する。失敗したら真値表へフォールバック。

    戻り値 (表, 公式取得に成功したか)。
    """
    if offline:
        log("  --offline 指定のため公式へは接続せず真値表を使用")
        return dict(EIA_HOLIDAY_TRUTH), False
    try:
        r = requests.get(EIA_SCHEDULE_URL, timeout=30, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        parsed = parse_schedule_html(r.text)
        if not parsed:
            raise ValueError("表を1行も抽出できなかった（ページ構造変更の疑い）")
        log(f"  EIA公式スケジュールから祝日繰り下げ {len(parsed)} 件を取得")
        return parsed, True
    except Exception as e:  # noqa: BLE001 ベストエフォート（落とさない）
        log(f"  EIA公式スケジュール取得に失敗（{type(e).__name__}: {e}）→ 真値表へフォールバック")
        record_fetch_warning(
            "fetch_eia",
            f"EIA公式スケジュールから取得できず真値表フォールバック使用（{len(EIA_HOLIDAY_TRUTH)} 件）",
        )
        return dict(EIA_HOLIDAY_TRUTH), False


# ----------------------------------------------------------------------
# イベント生成
# ----------------------------------------------------------------------
def wednesdays_in_window(today: date, back_days: int, forward_days: int) -> list[date]:
    """ローリング窓に入る水曜日を古い順に返す。"""
    start = today - timedelta(days=back_days)
    end = today + timedelta(days=forward_days)
    # start 以降で最初の水曜（月=0 … 水=2）
    first = start + timedelta(days=(2 - start.weekday()) % 7)
    out: list[date] = []
    d = first
    while d <= end:
        out.append(d)
        d += timedelta(days=7)
    return out


def build_eia_event(
    normal_wednesday: date,
    overrides: dict[date, tuple[date, int, int, str]],
) -> dict:
    """本来の公表水曜1つから events JSON 形式の1件を作る。"""
    week_ending = normal_wednesday - timedelta(days=5)  # 対象週の金曜
    override = overrides.get(normal_wednesday)
    if override is not None:
        release_date, hour, minute, holiday = override
        is_estimated = False   # 公式表に載っている＝一次情報で確定
    else:
        release_date, hour, minute, holiday = normal_wednesday, NORMAL_HOUR, NORMAL_MINUTE, ""
        is_estimated = True    # 毎週水曜のルール計算。後から繰り下げが公表されたら追従する

    local_dt = datetime(release_date.year, release_date.month, release_date.day, hour, minute, tzinfo=ET)
    utc_dt = local_dt.astimezone(UTC)

    description = (
        f"米エネルギー情報局(EIA)の週次石油在庫統計（対象週: {week_ending.isoformat()} まで）。"
        "原油・ガソリン・留出油の在庫増減が発表され、原油(WTI)価格が最も素直に反応する定例イベント。"
    )
    if holiday:
        description += f" ※{holiday}のため通常の水曜から繰り下げ（公式スケジュールによる確定日）。"

    return {
        "id": f"us_eia_{week_ending.isoformat()}",   # 対象週で固定（繰り下がっても不変）
        "title": "EIA週次原油在庫",
        "category": "EIA",
        "country": "US",
        "datetime_utc": utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "datetime_local": local_dt.isoformat(),
        "timezone": "America/New_York",
        "importance": 1,
        "is_estimated": is_estimated,
        "description": description,
        "source_url": EIA_REPORT_URL,
    }


def build_all(
    today: date,
    overrides: dict[date, tuple[date, int, int, str]],
    official_ok: bool,
    back_days: int = WINDOW_BACK_DAYS,
    forward_days: int = WINDOW_FORWARD_DAYS,
) -> list[dict]:
    wednesdays = wednesdays_in_window(today, back_days, forward_days)
    events = [build_eia_event(w, overrides) for w in wednesdays]

    # 真値表フォールバック中に窓がカバー範囲を追い越すと、祝日繰り下げを取りこぼす。
    # サイレントに誤日付を出さないよう警告を残す（health-watchdog が LINE 通知する）。
    if not official_ok and wednesdays and wednesdays[-1] > TRUTH_COVERED_UNTIL:
        record_fetch_warning(
            "fetch_eia",
            f"真値表のカバー範囲({TRUTH_COVERED_UNTIL})を超える週まで生成している"
            f"（最終 {wednesdays[-1]}）。祝日繰り下げを取りこぼす可能性あり",
        )
    events.sort(key=lambda e: e["datetime_utc"])
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description="EIA週次石油在庫統計の発表予定を生成")
    parser.add_argument("--self-test", action="store_true",
                        help="tmp に書かず生成結果を標準出力に表示")
    parser.add_argument("--offline", action="store_true",
                        help="公式へ接続せず真値表のみで生成（テスト用）")
    args = parser.parse_args()

    overrides, official_ok = fetch_holiday_overrides(offline=args.offline)
    today = datetime.now(ET).date()
    events = build_all(today, overrides, official_ok)
    shifted = sum(1 for e in events if not e["is_estimated"])
    log(f"fetch_eia: 基準日={today}（ET）、生成 {len(events)} 件"
        f"（うち祝日繰り下げ確定 {shifted} 件 / 公式取得={'成功' if official_ok else 'フォールバック'}）")

    if args.self_test:
        for ev in events:
            mark = "確定" if not ev["is_estimated"] else "予定"
            print(f"  {ev['datetime_local']} [{mark}] {ev['title']}  ({ev['id']})")
        return 0

    path = write_tmp("fetch_eia_out", {"events": events})
    log(f"  {path} に書き出し")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
