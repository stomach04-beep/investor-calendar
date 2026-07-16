"""
FRB FOMC カレンダーから今後の会合日を取得し、tmp/fetch_fomc_out.json に出力する。

ベストエフォート：パース失敗時は何も書かない（pipeline は build_events.py の出力で継続）。
成功時は is_estimated=False で events を出力する（公式確定情報）。

出力フォーマット:
[
  {"id": "us_fomc_2026-XX-XX", "datetime_utc": "...", "is_estimated": false, ...},
  ...
]
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import write_tmp, log, record_fetch_warning  # noqa: E402


FRB_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

# FOMC 開催月の英語名 → 月数字
MONTH_MAP = {
    "January": 1, "Jan": 1, "February": 2, "Feb": 2, "March": 3, "Mar": 3,
    "April": 4, "Apr": 4, "May": 5, "June": 6, "Jun": 6,
    "July": 7, "Jul": 7, "August": 8, "Aug": 8, "September": 9, "Sep": 9, "Sept": 9,
    "October": 10, "Oct": 10, "November": 11, "Nov": 11, "December": 12, "Dec": 12,
}


def fetch_html() -> str | None:
    try:
        r = requests.get(FRB_URL, timeout=30, headers={"User-Agent": "investor-calendar-bot/1.0"})
        if r.status_code == 200:
            return r.text
        log(f"  fetch_fomc: HTTP {r.status_code} を受信、スキップ")
        return None
    except Exception as e:
        log(f"  fetch_fomc: 取得失敗 ({type(e).__name__}: {e})、スキップ")
        return None


def parse_fomc_meetings(html: str) -> list[dict]:
    """
    FRB FOMC カレンダーHTMLから 2日目（声明発表日）を抽出する。

    HTML 構造例（年度ごとに panel-default ブロック）:
      <div class="panel panel-default">
        <div class="panel-heading">
          <h4>2026 FOMC Meetings</h4>
        </div>
        <div class="panel-body">
          <div class="row fomc-meeting">
            <div class="col-xs-3">
              <strong>January</strong>
            </div>
            <div class="col-xs-9">
              <p>27-28</p>  ← 末尾の数字が声明発表日
              ...
    """
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict] = []

    # 年ごとの panel を走査
    for panel in soup.select("div.panel.panel-default"):
        heading = panel.select_one(".panel-heading h4, .panel-heading h5, .panel-heading h2")
        if not heading:
            continue
        m = re.search(r"\b(20\d{2})\b", heading.get_text())
        if not m:
            continue
        year = int(m.group(1))

        # 各会合ブロック
        for meet in panel.select(".fomc-meeting"):
            month_el = meet.select_one(".fomc-meeting__month, .col-xs-3 strong, strong")
            date_el = meet.select_one(".fomc-meeting__date, .col-xs-9 p, p")
            if not month_el or not date_el:
                continue
            month_text = month_el.get_text(strip=True)
            date_text = date_el.get_text(" ", strip=True)

            # "Jan/Feb" 等、複数月にまたがる場合は最初の月を採用（実際はあまりない）
            month_token = re.split(r"[\s/–-]", month_text)[0]
            month = MONTH_MAP.get(month_token)
            if not month:
                continue

            # "27-28" 形式から末尾（声明発表日）を取り出す
            day_match = re.search(r"(\d{1,2})\s*[-–]\s*(\d{1,2})", date_text)
            if day_match:
                day = int(day_match.group(2))
            else:
                # 1日のみ（議事要旨等）の場合は対象外（FOMC声明は2日目）
                continue

            # タイムゾーン US/Eastern。声明発表は 14:00 ET。冬時間/夏時間は粗く EST/EDT 自動判定省略
            # 簡易: 1〜3月、11〜12月は EST(-05:00)、それ以外は EDT(-04:00) として ET 14:00 を UTC 換算
            if month in (1, 2, 3, 11, 12):
                offset = "-05:00"
                utc_hour = 19  # 14 ET + 5
            else:
                offset = "-04:00"
                utc_hour = 18  # 14 ET + 4
            datetime_local = f"{year:04d}-{month:02d}-{day:02d}T14:00:00{offset}"
            datetime_utc = f"{year:04d}-{month:02d}-{day:02d}T{utc_hour:02d}:00:00Z"
            month_label = {1: "1月", 2: "2月", 3: "3月", 4: "4月", 5: "5月", 6: "6月",
                           7: "7月", 8: "8月", 9: "9月", 10: "10月", 11: "11月", 12: "12月"}[month]
            ev_id = f"us_fomc_{year:04d}-{month:02d}-{day:02d}"
            title = f"FOMC声明発表 ({month_label}会合)"
            events.append({
                "id": ev_id,
                "title": title,
                "category": "FOMC",
                "country": "US",
                "datetime_utc": datetime_utc,
                "datetime_local": datetime_local,
                "timezone": "America/New_York",
                "importance": 3,
                "is_estimated": False,
                "description": "2日目14:00 ET発表、議長会見14:30 ET。",
                "source_url": FRB_URL,
            })
    return events


def load_target_years() -> set[int]:
    """build_events_out.json の covered_years を読み込む。なければ今年と来年。"""
    try:
        from common import load_tmp  # noqa: E402
        data = load_tmp("build_events_out")
        years = set(int(y) for y in data.get("covered_years", []))
        if years:
            return years
    except Exception:
        pass
    now_year = datetime.now(timezone.utc).year
    return {now_year, now_year + 1}


def main() -> int:
    html = fetch_html()
    if not html:
        log("fetch_fomc: FRBサイト取得失敗 → 出力ファイルなし（後続は build_events.py 出力のみで継続）")
        record_fetch_warning("fetch_fomc", "FRBサイト取得失敗 → シード値で継続")
        return 0
    try:
        events = parse_fomc_meetings(html)
    except Exception as e:
        log(f"fetch_fomc: HTML パース失敗 ({type(e).__name__}: {e}) → 出力ファイルなし")
        record_fetch_warning("fetch_fomc", f"HTMLパース失敗 ({type(e).__name__}) → シード値で継続")
        return 0
    if not events:
        log("fetch_fomc: FOMC 会合が抽出できず、スキップ")
        record_fetch_warning("fetch_fomc", "抽出ゼロ（サイト構造変化の疑い） → シード値で継続")
        return 0
    # target_years でフィルタ：build_events がカバーしている年のみ採用
    target_years = load_target_years()
    filtered = [e for e in events if int(e["id"][8:12]) in target_years]
    log(f"fetch_fomc: 全 {len(events)} 件 → target_years={sorted(target_years)} で {len(filtered)} 件に絞り込み")
    if not filtered:
        log("fetch_fomc: target_years 内の FOMC 会合がなくスキップ")
        return 0
    path = write_tmp("fetch_fomc_out", {"events": filtered, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    log(f"fetch_fomc: {len(filtered)} 件の FOMC 会合を {path} に書き出し")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
