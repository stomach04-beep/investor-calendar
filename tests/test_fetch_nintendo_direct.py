# -*- coding: utf-8 -*-
"""fetch_nintendo_direct.py の告知見出しパーサの試験。

ここで守りたいこと:
  放送時刻を1つ取り違えると「反応する営業日」が1日ずれて、カレンダーの意味が反転する。
  とくに「午後11時」を11時（場中）と読むと、引け後の放送を場中の放送として扱ってしまう。
  実在の告知見出し（ファミ通・4Gamer）をそのまま入れて回帰させる。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_nintendo_direct as F  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")


def _now(date_str: str) -> datetime:
    """告知が出た日（放送の1〜2日前）を now として渡す。年の解決が効くようにする。"""
    y, m, d = (int(x) for x in date_str.split("-"))
    return datetime(y, m, d, 12, 0, tzinfo=JST)


# (見出し, 告知が出た日, 期待する放送日, 期待する時刻HHMM, 本体ダイレクトか)
ANNOUNCEMENTS = [
    ("【ニンダイ】ニンテンドーダイレクト9月9日（水）に実施。23時より約45分間。"
     "前日8日には“『ゼルダの伝説』40周年ダイレクト”も",
     "2026-09-07", "2026-09-09", 2300, True),
    ("【ニンダイ】Nintendo Directが6月9日（火）23時に配信決定。配信時間は約50分。",
     "2026-06-05", "2026-06-09", 2300, True),
    ("【ニンダイ】Nintendo Direct ソフトメーカーラインナップが2月5日（木）23時に配信決定。"
     "放送時間は約30分",
     "2026-02-03", "2026-02-05", 2300, False),
    ("“Nintendo Direct”9月13日23時より配信決定。Switchの今冬発売タイトルを中心にした約40分の放送",
     "2022-09-12", "2022-09-13", 2300, True),
    # 「午後11時」= 23時。11時（場中）と読んではいけない。
    # なお見出しは「Nintendo Direct」だけだが中身はソフトメーカーラインナップ
    # （Partner Showcase）なので本体ダイレクトではない＝★は付けない
    ("【ニンダイ】“Nintendo Direct（ニンテンドーダイレクト）”が2月21日午後11時より配信決定。"
     "各ソフトメーカーからのタイトルを特集",
     "2024-02-20", "2024-02-21", 2300, False),
    # 「午前7時」= 7時。寄り付き前なので反応は当日
    ("Nintendo Directが2月18日午前7時から放映。『スマブラSP』やSwitchの2021年上半期発売予定のソフトを紹介",
     "2021-02-16", "2021-02-18", 700, True),
    ("【ニンダイ】Nintendo Directが9月12日（金）22時から配信決定。Switch2＆Switchの新作を届ける約60分",
     "2025-09-10", "2025-09-12", 2200, True),
]

# 拾ってはいけない見出し（放送後のまとめ・関係ない記事）
NON_ANNOUNCEMENTS = [
    ("【ニンダイまとめ】「Nintendo Direct 2026.9.9」発表内容まとめ！", "2026-09-10"),
    ("「星のカービィ ワールドビヨンド」など新作多数の「Nintendo Direct 2026.9.9」まとめ", "2026-09-10"),
    ("『ドラゴンクエストモンスターズ4』の体験版がNintendo Direct終了後に配信に", "2026-09-10"),
    ("「ファイアーエムブレム 万紫千紅 Direct 2026.8.4」を公開。楽曲配信やコミック連載などの最新情報も",
     "2026-08-05"),
    # 日付はあるが時刻が無い＝反応日が決まらないので使わない
    ("【ニンダイ】Nintendo Directを9月9日に配信決定", "2026-09-07"),
    # ゲームと無関係
    ("日経平均が3万円台を回復。9月9日の東京株式市場は23時までの時間外取引で…", "2026-09-08"),
]


def test_announcements_parsed():
    for title, when, want_date, want_hhmm, want_main in ANNOUNCEMENTS:
        got = F.parse_announcement(title, _now(when))
        assert got is not None, f"拾えていない: {title}"
        date_jst, hhmm, is_main = got
        assert date_jst == want_date, f"{title}\n  放送日 {date_jst} != {want_date}"
        assert hhmm == want_hhmm, f"{title}\n  時刻 {hhmm} != {want_hhmm}"
        assert is_main == want_main, f"{title}\n  本体判定 {is_main} != {want_main}"


def test_non_announcements_ignored():
    for title, when in NON_ANNOUNCEMENTS:
        got = F.parse_announcement(title, _now(when))
        assert got is None, f"拾ってはいけない見出しを拾った: {title} -> {got}"


def test_reaction_note_splits_on_market_close():
    """反応する営業日の説明が、引け後(15:00以降)と寄り前(9:00未満)で分かれること。"""
    assert "翌営業日" in F.reaction_note(2300)
    assert "翌営業日" in F.reaction_note(2200)
    assert "翌営業日" in F.reaction_note(1500)
    assert "当日" in F.reaction_note(700)
    assert "当日" in F.reaction_note(100)
    # 取引時間中の放送は当日扱い
    assert "当日" in F.reaction_note(1000)


def test_confirmed_directs_are_consistent():
    """固定表の放送日・時刻が壊れていないこと（id とイベントの突合）。"""
    events, _, _ = F.build_all(_now("2026-09-10"))
    ids = [e["id"] for e in events]
    assert len(ids) == len(set(ids)), "id が重複している"
    for date_jst, hhmm, name, is_main in F.CONFIRMED_DIRECTS:
        ev = next(e for e in events if e["id"] == F.ID_PREFIX + date_jst)
        assert ev["datetime_local"].startswith(date_jst), ev["id"]
        assert ev["datetime_local"][11:16] == f"{hhmm // 100:02d}:{hhmm % 100:02d}", ev["id"]
        assert ev["category"] == "GAMEEVENT"
        assert ev["country"] == "JP"
        # 放送日時は公式確定なので保護対象（notion_upsert が上書きしない）
        assert ev["is_estimated"] is False, ev["id"]
        assert ev["importance"] == (F.IMPORTANCE_MAIN if is_main else F.IMPORTANCE_SUB)
        assert name in ev["title"]
