"""
日銀 金融政策決定会合スケジュールから今後の会合日を取得し、tmp/fetch_boj_out.json に出力する。

ベストエフォート：パース失敗時は何も書かない。
成功時は is_estimated=False で events を出力する。
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


BOJ_URL = "https://www.boj.or.jp/mopo/mpmsche_minu/index.htm"

# 月名（日本語）→ 月番号
JP_MONTH_MAP = {
    "1月": 1, "2月": 2, "3月": 3, "4月": 4, "5月": 5, "6月": 6,
    "7月": 7, "8月": 8, "9月": 9, "10月": 10, "11月": 11, "12月": 12,
}


def fetch_html() -> str | None:
    try:
        r = requests.get(BOJ_URL, timeout=30, headers={
            "User-Agent": "investor-calendar-bot/1.0",
            "Accept-Language": "ja,en;q=0.9",
        })
        if r.status_code == 200:
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        log(f"  fetch_boj: HTTP {r.status_code}、スキップ")
        return None
    except Exception as e:
        log(f"  fetch_boj: 取得失敗 ({type(e).__name__}: {e})、スキップ")
        return None


def parse_boj_meetings(html: str) -> list[dict]:
    """
    日銀MPMスケジュール表から「決定会合 + 議事公表」の組から決定会合の最終日を抽出する。

    日銀の表構造は年によって変わるため、ベストエフォート：
    - 「YYYY年」見出しの後の表
    - 行に「N月D日（曜）、D日（曜）」のような形で開催期間
    - 「12:00頃」「正午頃」などの結果発表時刻
    """
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict] = []

    # ページ全テキストから "YYYY年MM月DD日、DD日" のような開催期間を抽出
    # 正規表現で粗くマッチ：「N月D～E日」「N月D日、E日」など
    text = soup.get_text("\n", strip=True)

    # 年見出しは "YYYY年" の単体行
    year_pattern = re.compile(r"^(20\d{2})年$", re.MULTILINE)
    year_matches = list(year_pattern.finditer(text))

    if not year_matches:
        # 年見出しが取れなければ別パターン
        log("  fetch_boj: 年見出しが見つからずスキップ")
        return events

    # 年見出しごとに区切って会合行を抽出
    for i, ym in enumerate(year_matches):
        year = int(ym.group(1))
        start = ym.end()
        end = year_matches[i + 1].start() if i + 1 < len(year_matches) else len(text)
        chunk = text[start:end]

        # 開催期間パターン：
        #   1月22日、23日       → 2日目（23日）を採用
        #   1月22日 ～ 23日     → 2日目（23日）
        #   1月23日             → 1日のみ（稀）
        # 月をまたぐ12月→1月は別途扱い：このケースは年見出しの影響でズレるが今は無視
        # 行内で月と日を取り出す
        for line in chunk.split("\n"):
            line = line.strip()
            if not line:
                continue
            m_month = re.search(r"(\d{1,2})月", line)
            if not m_month:
                continue
            month = int(m_month.group(1))
            # 日付候補をすべて拾う
            day_candidates = re.findall(r"(\d{1,2})日", line)
            if not day_candidates:
                continue
            try:
                days = [int(d) for d in day_candidates]
            except ValueError:
                continue
            # 最終日 = 結果発表日
            end_day = max(days)
            if not (1 <= end_day <= 31):
                continue

            # 重複チェック（同月で複数行ヒットしないように）
            key = (year, month, end_day)
            if any(e.get("_key") == key for e in events):
                continue

            datetime_local = f"{year:04d}-{month:02d}-{end_day:02d}T12:00:00+09:00"
            # UTC = JST - 9h
            jst_dt = datetime.fromisoformat(datetime_local)
            utc_dt = jst_dt.astimezone(timezone.utc)
            datetime_utc = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            month_label = f"{month}月"
            events.append({
                "_key": key,  # 後で除去
                "id": f"jp_boj_{year:04d}-{month:02d}-{end_day:02d}",
                "title": f"日銀金融政策決定会合 ({month_label}会合)",
                "category": "BOJ",
                "country": "JP",
                "datetime_utc": datetime_utc,
                "datetime_local": datetime_local,
                "timezone": "Asia/Tokyo",
                "importance": 3,
                "is_estimated": False,
                "description": "結果発表は最終日の正午頃、総裁会見は15:30頃。",
                "source_url": BOJ_URL,
            })

    # _key 削除
    for ev in events:
        ev.pop("_key", None)
    return events


def load_seed_boj_ids() -> set[str]:
    """build_events_out.json から BOJ カテゴリのイベントIDを集めて返す。"""
    try:
        from common import load_tmp  # noqa: E402
        data = load_tmp("build_events_out")
        return {e["id"] for e in data.get("events", []) if e.get("category") == "BOJ"}
    except Exception:
        return set()


def load_target_years() -> set[int]:
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
        log("fetch_boj: 日銀サイト取得失敗 → 出力ファイルなし")
        record_fetch_warning("fetch_boj", "日銀サイト取得失敗 → シード値で継続")
        return 0
    try:
        events = parse_boj_meetings(html)
    except Exception as e:
        log(f"fetch_boj: HTML パース失敗 ({type(e).__name__}: {e}) → 出力ファイルなし")
        record_fetch_warning("fetch_boj", f"HTMLパース失敗 ({type(e).__name__}) → シード値で継続")
        return 0
    if not events:
        log("fetch_boj: 会合が抽出できず、スキップ")
        record_fetch_warning("fetch_boj", "抽出ゼロ（サイト構造変化の疑い） → シード値で継続")
        return 0

    # 日銀ページは「決定会合日」「議事要旨公表日」「主な意見公表日」が混在するため、
    # シードに既存のBOJ ID と完全一致するものだけを採用する（既存IDの確定日付更新）。
    seed_ids = load_seed_boj_ids()
    target_years = load_target_years()
    filtered = [e for e in events
                if int(e["id"][7:11]) in target_years
                and e["id"] in seed_ids]
    log(f"fetch_boj: 全 {len(events)} 件 → seed BOJ id と一致＆target_years 内で {len(filtered)} 件")
    if not filtered:
        log("fetch_boj: seed と一致する BOJ 確定情報が抽出できずスキップ（後続は build_events のシード値で継続）")
        record_fetch_warning("fetch_boj", "seed一致ゼロ（サイト構造変化またはID体系変化の疑い） → シード値で継続")
        return 0

    path = write_tmp("fetch_boj_out", {"events": filtered, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    log(f"fetch_boj: {len(filtered)} 件の日銀会合を {path} に書き出し")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
