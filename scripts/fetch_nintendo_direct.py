"""
ニンテンドーダイレクトの放送予定を投資家カレンダーへ生成する。

なぜカレンダーに載せるか:
  任天堂(7974)はダイレクト放送の直後に動く。検証110b（jquants-bulk）の実測では、
  夜22〜23時放送の本体ダイレクト8回で、放送後最初の引けの株価が平均-2.00%・下落7/8回。
  「いつ放送があるか」を事前に知らないと身動きが取れないので、カレンダーに載せる。

放送時刻が決定的に重要（ここを外すと反応日が1日ずれる）:
  東証は9:00-15:00。同じ放送日Dでも
    - 15:00以降の放送（23時など）→ Dの東証は引け後なので **反応は翌営業日**
    - 9:00より前の放送（朝7時・深夜1時）→ Dの場中がすでに放送後なので **反応は当日**
  ダイレクトは2022年9月を境に「朝7時」から「夜22〜23時」へ切り替わっている。
  そのため放送時刻を description に必ず書き、反応する営業日も明記する。

2系統で作る:
  (1) CONFIRMED_DIRECTS … 放送日・時刻を一次情報（ファミ通の各告知記事）で確認済の過去分。
      データが動かないので固定表で持つ。
  (2) 告知スキャン（ベストエフォート）… ダイレクトは1〜3日前にしか告知されない。
      任天堂公式トピックスは「放送後の報告記事」しか出さないため事前告知が取れない。
      そこでゲームニュースのRSS（4Gamer・電ファミ）から告知記事を拾い、
      日付と時刻を抽出して未来の回を生成する。取得できなければ警告を残す
      （common.record_fetch_warning → sync.yml が ::warning:: で可視化）。

AI / Claude 呼び出しなし＝別枠クレジット消費ゼロ（RSSとHTTPのみ）。

出力: tmp/fetch_nintendo_direct_out.json（events 配列を含む辞書）
      → notion_upsert.py が build_events_out にマージして Notion へ upsert

実行:
    python scripts/fetch_nintendo_direct.py              # tmp に書き出し
    python scripts/fetch_nintendo_direct.py --self-test  # 書かずに標準出力へ
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import log, record_fetch_warning, write_tmp  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
CATEGORY = "GAMEEVENT"
ID_PREFIX = "nintendo_direct_"

# 本体ダイレクト（副題なしの Nintendo Direct / E3 / 本体世代）は市場が見ているので ★★☆、
# タイトル別・ソフトメーカーラインナップ・Indie World は ★☆☆
IMPORTANCE_MAIN = 2
IMPORTANCE_SUB = 1

# ----------------------------------------------------------------------
# (1) 確認済の過去分
#     (放送日JST, 放送時刻HHMM, 番組名, 本体かどうか)
#     時刻の出典はファミ通の各告知記事（2026-09-10 に個別確認）
# ----------------------------------------------------------------------
CONFIRMED_DIRECTS = [
    ("2021-02-18",  700, "Nintendo Direct 2021.2.18", True),
    ("2021-06-16",  100, "Nintendo Direct: E3 2021", True),
    ("2021-09-24",  700, "Nintendo Direct 2021.9.24", True),
    ("2022-02-10",  700, "Nintendo Direct 2022.2.10", True),
    ("2022-09-13", 2300, "Nintendo Direct 2022.9.13", True),
    ("2023-02-09",  700, "Nintendo Direct 2023.2.9", True),
    ("2023-06-21", 2300, "Nintendo Direct 2023.6.21", True),
    ("2023-09-14", 2300, "Nintendo Direct 2023.9.14", True),
    ("2024-06-18", 2300, "Nintendo Direct 2024.6.18", True),
    ("2025-03-27", 2300, "Nintendo Direct 2025.3.27", True),
    ("2025-04-02", 2200, "Nintendo Direct: Nintendo Switch 2 2025.4.2", True),
    ("2025-09-12", 2200, "Nintendo Direct 2025.9.12", True),
    ("2026-06-09", 2300, "Nintendo Direct 2026.6.9", True),
    ("2026-09-09", 2300, "Nintendo Direct 2026.9.9", True),
]

# ----------------------------------------------------------------------
# (2) 告知スキャンの設定
# ----------------------------------------------------------------------
RSS_FEEDS = [
    ("4Gamer", "https://www.4gamer.net/rss/index.xml"),
    ("電ファミニコゲーマー", "https://news.denfaminicogamer.jp/feed"),
]
UA = {"User-Agent": "Mozilla/5.0 (investor-calendar fetch_nintendo_direct)"}
HTTP_TIMEOUT = 20

# 番組名らしさ（これが無い記事は無視）
RE_PROGRAM = re.compile(r"(ニンテンドーダイレクト|Nintendo Direct|ニンダイ|Indie World|インディーワールド)")
# 告知であることの目印（放送後のまとめ記事を除くため必須）
RE_ANNOUNCE = re.compile(
    r"(配信決定|配信が決定|配信します|配信予定|放送決定|放映|放送|実施|開催決定|お届けします)")
# 放送後のまとめ記事を積極的に除外
RE_RECAP = re.compile(r"(まとめ|発表内容|を公開|振り返)")
# 「9月9日（水）23時」「9月9日 23時00分」「9/9 23:00」から日付と時刻を取る
RE_DATE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")
RE_TIME = re.compile(r"(午前|午後)?\s*(\d{1,2})\s*時(?:\s*(\d{1,2})\s*分)?")
# 本体ダイレクト判定（副題が付くものはタイトル別扱い）
RE_MAIN = re.compile(r"^(?:ニンテンドーダイレクト|Nintendo Direct)\s*(?:\d|が|は|を|の|、|$)")
# 見出し冒頭の【カテゴリ】や引用符は判定の邪魔になるので外す
RE_LEAD_BRACKET = re.compile(r"^(?:【[^】]*】|\s)+")
# 「Nintendo Direct（ニンテンドーダイレクト）が…」の括弧内の言い換えを落とす
RE_PAREN_ALIAS = re.compile(r"[（(][^）)]{0,30}[）)]")
# 本体ダイレクトでない回の目印（見出しが Nintendo Direct だけでもこれらがあれば本体扱いしない）
RE_NOT_MAIN = re.compile(r"(ソフトメーカー|Partner Showcase|mini|ミニ|Indie World|インディーワールド)")
# 引用符は番組名の前後どちらにも付くので、判定前に全部落とす
QUOTE_CHARS = "“”「」『』〝〟‘’'\""

SCAN_AHEAD_DAYS = 60     # これより先の日付は誤抽出とみなす
SCAN_BACK_DAYS = 2       # 直前に告知された当日・前日ぶんも拾う


def fetch_text(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        log(f"  RSS取得失敗 {url}: {type(e).__name__}: {e}")
        return None


def parse_announcement(title: str, now: datetime) -> tuple[str, int, bool] | None:
    """告知記事のタイトルから (放送日YYYY-MM-DD, 時刻HHMM, 本体か) を取る。取れなければ None。"""
    if not RE_PROGRAM.search(title) or not RE_ANNOUNCE.search(title):
        return None
    if RE_RECAP.search(title):
        return None          # 「発表内容まとめ」等は放送後の記事
    md = RE_DATE.search(title)
    tm = RE_TIME.search(title)
    if not md or not tm:
        return None          # 日付と時刻の両方が無い告知は使わない（反応日が決まらない）
    month, day = int(md.group(1)), int(md.group(2))
    ampm, hh = tm.group(1), int(tm.group(2))
    mm = int(tm.group(3)) if tm.group(3) else 0
    # 「午後11時」=23時。ここを取り違えると引け後か寄り前かが逆になり反応日が1日ずれる
    if ampm == "午後" and hh < 12:
        hh += 12
    elif ampm == "午前" and hh == 12:
        hh = 0
    if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    # 年はタイトルに無いので「今日に最も近い同月日」を採る（年末年始の跨ぎに対応）
    best = None
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            cand = datetime(year, month, day, tzinfo=JST)
        except ValueError:
            continue
        delta = (cand.date() - now.date()).days
        if -SCAN_BACK_DAYS <= delta <= SCAN_AHEAD_DAYS:
            if best is None or abs(delta) < abs(best[1]):
                best = (cand, delta)
    if best is None:
        return None
    clean = RE_LEAD_BRACKET.sub("", title)
    clean = clean.translate({ord(q): None for q in QUOTE_CHARS}).lstrip()
    clean = RE_PAREN_ALIAS.sub("", clean).lstrip()
    is_main = bool(RE_MAIN.match(clean)) and not RE_NOT_MAIN.search(title)
    return best[0].strftime("%Y-%m-%d"), hh * 100 + mm, is_main


def scan_announcements(now: datetime) -> tuple[list[tuple[str, int, str, bool]], bool]:
    """RSSから未来の放送予定を拾う。戻り値 (予定リスト, 1本でもRSSが読めたか)。"""
    found: dict[str, tuple[str, int, str, bool]] = {}
    any_feed_ok = False
    for name, url in RSS_FEEDS:
        text = fetch_text(url)
        if text is None:
            continue
        any_feed_ok = True
        titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", text, re.S)
        hit = 0
        for raw in titles:
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw)).strip()
            parsed = parse_announcement(title, now)
            if not parsed:
                continue
            date_jst, hhmm, is_main = parsed
            hit += 1
            # 同じ放送日は最初に拾ったものを採用（媒体間の重複を潰す）
            found.setdefault(date_jst, (date_jst, hhmm, title, is_main))
        log(f"  {name}: 記事{len(titles)}件 → 告知候補{hit}件")
    return sorted(found.values()), any_feed_ok


# ----------------------------------------------------------------------
# イベント生成
# ----------------------------------------------------------------------
def reaction_note(hhmm: int) -> str:
    """放送時刻から「反応する営業日」の説明文を作る（ここが唯一の定義）。"""
    if hhmm >= 1500:
        return "放送は東証の引け後なので、株価の反応は「翌営業日」。放送日の引けがイベント直前の値段になる"
    if hhmm < 900:
        return "放送は東証の寄り付き前なので、株価の反応は「当日の場中」。前営業日の引けがイベント直前の値段になる"
    return "放送が東証の取引時間中（9:00-15:00）なので、株価の反応は「当日の場中」"


def build_event(date_jst: str, hhmm: int, name: str, is_main: bool,
                source_url: str | None) -> dict:
    local = datetime(int(date_jst[0:4]), int(date_jst[5:7]), int(date_jst[8:10]),
                     hhmm // 100, hhmm % 100, tzinfo=JST)
    desc = (
        f"{name}（放送 {hhmm // 100}:{hhmm % 100:02d} JST）。\n"
        f"{reaction_note(hhmm)}。\n"
        "任天堂(7974)はダイレクト直後に動く。検証110bの実測では夜22〜23時放送の"
        "本体ダイレクト8回で、放送後最初の引けの株価が平均-2.00%・下落7/8回。"
        "ただし標本8件の事後仕様のため前向き検証中（検証110c）で、実弾は入れない方針。"
    )
    return {
        "id": f"{ID_PREFIX}{date_jst}",
        "title": name,
        "category": CATEGORY,
        "country": "JP",
        "datetime_utc": local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "datetime_local": local.isoformat(),
        "timezone": "Asia/Tokyo",
        "importance": IMPORTANCE_MAIN if is_main else IMPORTANCE_SUB,
        # 放送日時は公式に確定しているので false（notion_upsert の保護対象＝手修正が守られる）
        "is_estimated": False,
        "description": desc,
        "source_url": source_url,
    }


def build_all(now: datetime) -> tuple[list[dict], bool, int]:
    by_id: dict[str, dict] = {}
    for date_jst, hhmm, name, is_main in CONFIRMED_DIRECTS:
        ev = build_event(date_jst, hhmm, name, is_main, None)
        by_id[ev["id"]] = ev
    n_confirmed = len(by_id)

    scanned, any_feed_ok = scan_announcements(now)
    for date_jst, hhmm, title, is_main in scanned:
        ev_id = f"{ID_PREFIX}{date_jst}"
        if ev_id in by_id:
            continue          # 固定表が正（時刻を一次情報で確認済）
        name = title if len(title) <= 60 else title[:57] + "..."
        by_id[ev_id] = build_event(date_jst, hhmm, name, is_main, None)
    n_new = len(by_id) - n_confirmed
    return sorted(by_id.values(), key=lambda e: e["datetime_utc"]), any_feed_ok, n_new


def main() -> int:
    parser = argparse.ArgumentParser(description="ニンテンドーダイレクトの放送予定を生成")
    parser.add_argument("--self-test", action="store_true",
                        help="tmp に書かず生成結果を標準出力に表示")
    args = parser.parse_args()

    now = datetime.now(JST)
    events, any_feed_ok, n_new = build_all(now)
    log(f"fetch_nintendo_direct: 確定{len(CONFIRMED_DIRECTS)}件 + 告知スキャン{n_new}件 "
        f"= 計{len(events)}件")

    # サイレント失敗対策: 固定表があるので「0件」にはならない＝RSSが死んでも緑に見える。
    # RSSが1本も読めなかったときは必ず警告を残す（sync.yml が ::warning:: で出す）
    if not any_feed_ok:
        record_fetch_warning(
            "fetch_nintendo_direct",
            "告知スキャン用のRSSを1本も取得できなかった。過去の確定分のみ出力している"
            "＝新しい放送予定が載っていない可能性がある")
    future = [e for e in events if e["datetime_local"] >= now.isoformat()]
    log(f"  うち未来の予定 {len(future)}件")

    if args.self_test:
        for ev in events:
            mark = "★" if ev["importance"] == IMPORTANCE_MAIN else " "
            print(f"  {mark} {ev['datetime_local'][:16]} {ev['title']}  ({ev['id']})")
        print(f"\nRSS取得: {'成功' if any_feed_ok else '全滅'} / 未来の予定 {len(future)}件")
        return 0

    path = write_tmp("fetch_nintendo_direct_out", {"events": events})
    log(f"  {path} に書き出し")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
