"""
data/investor_events.json のシードデータを機械的に修正する冪等スクリプト。

【重要・ID不変ポリシー】
  Notion 側の既存ページは notion_upsert.py が `userDefined:ID`(rich_text) をキーに
  upsert する。つまり id は「不変の識別子」であり、ユーザーが Notion 上で発表日時を
  手修正しても id は旧値のまま残っている（例: 全国CPI(5月分)=jp_cpi_2026-06-26）。
  そのため本スクリプトは id・title を絶対に書き換えない。発表日時の正しさは
  datetime_utc / datetime_local が担保するので、id 末尾の日付と実際の発表日がズレても
  Notion 表示は正しくなる（既存運用と同じ）。

修正対象（CLAUDE.md の「時刻の二重適用ミス」教訓を踏まえ、時刻はハードコードで固定する）:

  1. 全国CPI (country=JP, category=CPI) 全件:
       - 発表日 = 「対象月の翌月の、19日を含む週(月曜起算 Mon〜Sun)の金曜日」で再計算
         （対象月は title の "(X月分)" から復元。年跨ぎは id の発表年で補正）
       - datetime_local を「当日 08:30:00+09:00」に（旧データは 23:30 に壊れている＝最重要バグ）
       - datetime_utc を「前日 23:30:00Z」に（08:30 JST = 前日 23:30 UTC）
       - ★ id・title は変更しない

  2. 米PCE (category=PCE):
       - title の "(X月分)" を見て BEA真値表（5/6/7/8/11月分・2026年）に該当する月は
         正しい発表日に修正。時刻は 8:30 ET 固定、offset は夏冬で zoneinfo から決定、
         datetime_utc を再計算。
       - 真値表に無い月（過去月等）は触らない。
       - ★ id・title は変更しない

  3. 米CPI (category=CPI, country=US):
       - 1月分が 2026-02-11 になっていれば datetime を 2026-02-13 08:30 ET に修正（BLS真値表）。
       - それ以外の月は触らない（前回行った「id を local日付に揃える」整合は取り消し）。
       - ★ id・title は変更しない

最後に local + offset == utc が機械的に整合するか全件検算し、ズレがあれば例外で止める。

実行:
    python scripts/_fix_seed_dates.py
冪等なので何度実行しても同じ結果になる。
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

# scripts/ を import path に入れて common を使う
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_canonical_events, write_canonical_events, log  # noqa: E402


# ----------------------------------------------------------------------
# タイムゾーン定義
# ----------------------------------------------------------------------
TZ_JST = ZoneInfo("Asia/Tokyo")
TZ_ET = ZoneInfo("America/New_York")


# ----------------------------------------------------------------------
# 全国CPI 公表日の計算ルール
# ----------------------------------------------------------------------
def jp_cpi_publish_date(target_year: int, target_month: int) -> dt.date:
    """
    全国CPI（総務省）の公表日を計算する。

    ルール: 対象月の「翌月」の、19日を含む週(月曜起算 Mon〜Sun)の金曜日。
    例: 対象=2026年5月分 → 翌月6月 → 6/19を含む週の金曜 = 2026-06-19。
    """
    # 翌月（年跨ぎ対応）
    pub_year = target_year
    pub_month = target_month + 1
    if pub_month > 12:
        pub_month = 1
        pub_year += 1
    # 翌月の19日
    d19 = dt.date(pub_year, pub_month, 19)
    # 月曜起算で19日を含む週の月曜日（weekday: Mon=0 .. Sun=6）
    monday = d19 - dt.timedelta(days=d19.weekday())
    # その週の金曜日 = 月曜 + 4
    return monday + dt.timedelta(days=4)


# ----------------------------------------------------------------------
# US PCE 真値表（BEA・2026年・裏取り済み）
# title の対象月 → 公表日(date)
# ----------------------------------------------------------------------
US_PCE_TRUTH_2026: dict[int, dt.date] = {
    5: dt.date(2026, 6, 25),    # 5月分 → 2026-06-25(木)
    6: dt.date(2026, 7, 30),    # 6月分 → 2026-07-30(木)
    7: dt.date(2026, 8, 26),    # 7月分 → 2026-08-26(水)
    8: dt.date(2026, 9, 30),    # 8月分 → 2026-09-30(水)
    11: dt.date(2026, 12, 23),  # 11月分 → 2026-12-23(水)
}

# US CPI 真値表（BLS・2026年）：title対象月 → 公表日。1月分の誤り修正に使う。
US_CPI_TRUTH_2026: dict[int, dt.date] = {
    1: dt.date(2026, 2, 13),  # 1月分 → 2026-02-13(金)（旧データは 02-11 で誤り）
}


# ----------------------------------------------------------------------
# ユーティリティ
# ----------------------------------------------------------------------
def et_offset_str(date: dt.date, hour: int = 8, minute: int = 30) -> str:
    """指定日の ET(America/New_York) のUTCオフセット文字列（例 '-04:00'）を返す。"""
    aware = dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=TZ_ET)
    off = aware.utcoffset()
    assert off is not None
    total_min = int(off.total_seconds() // 60)
    sign = "+" if total_min >= 0 else "-"
    total_min = abs(total_min)
    return f"{sign}{total_min // 60:02d}:{total_min % 60:02d}"


def to_utc_z(local_iso: str) -> str:
    """オフセット付き ISO 文字列を UTC の 'YYYY-MM-DDTHH:MM:SSZ' に変換する。"""
    aware = dt.datetime.fromisoformat(local_iso)
    utc = aware.astimezone(dt.timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def title_target_month(title: str) -> int | None:
    """title の '(X月分)' から対象月の数字を取り出す。"""
    m = re.search(r"(\d{1,2})月分", title)
    return int(m.group(1)) if m else None


def id_date(event_id: str) -> dt.date:
    """id 末尾の YYYY-MM-DD を date にする（対象年の補正にのみ使う。id 自体は変更しない）。"""
    ymd = event_id.split("_")[-1]
    y, m, d = [int(x) for x in ymd.split("-")]
    return dt.date(y, m, d)


def jp_cpi_target_ym(ev: dict) -> tuple[int, int]:
    """
    全国CPI イベントの対象(年, 月)を復元する。

    - title が "(2025年12月分)" のように年号付きならそれを使う。
    - title が "(X月分)" のように年号なしなら、id 末尾の発表年から対象年を推定
      （対象月 <= 発表月 なら同年、対象月 > 発表月 なら前年）。
    id 末尾の日付は「発表月の手掛かり」として参照するだけで、id 文字列は一切書き換えない。
    """
    title = ev.get("title", "")
    # "(2025年12月分)" 形式（年号付き）
    m = re.search(r"\((\d{4})年(\d{1,2})月分\)", title)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    # "(X月分)" 形式（年号なし）→ id の発表年月から対象年を補正
    tgt_m = title_target_month(title)
    cur = id_date(ev["id"])
    pub_y, pub_m = cur.year, cur.month
    if tgt_m is None:
        # title から月が取れない場合は id の発表月 - 1 を対象月とみなす
        tgt_m = pub_m - 1
        tgt_y = pub_y
        if tgt_m < 1:
            tgt_m = 12
            tgt_y -= 1
        return (tgt_y, tgt_m)
    tgt_y = pub_y if tgt_m <= pub_m else pub_y - 1
    return (tgt_y, tgt_m)


# ----------------------------------------------------------------------
# 各カテゴリの修正処理（id・title は不変）
# ----------------------------------------------------------------------
def fix_jp_cpi(ev: dict, changes: list[str]) -> None:
    """全国CPI 1件の発表日時のみ修正する（08:30 JST固定）。id・title は変更しない。"""
    tgt_y, tgt_m = jp_cpi_target_ym(ev)
    new_date = jp_cpi_publish_date(tgt_y, tgt_m)

    old_local = ev["datetime_local"]
    old_utc = ev["datetime_utc"]

    # datetime_local = 当日 08:30:00+09:00 固定
    new_local = f"{new_date.strftime('%Y-%m-%d')}T08:30:00+09:00"
    # datetime_utc = 前日 23:30:00Z（8:30 JST = 前日23:30 UTC）固定
    new_utc = to_utc_z(new_local)

    if (new_local, new_utc) != (old_local, old_utc):
        changes.append(
            f"  [JP CPI] {ev['title']}  (id 据置: {ev['id']})\n"
            f"      local: {old_local} -> {new_local}\n"
            f"      utc  : {old_utc} -> {new_utc}"
        )
    # ★ id・title は触らない。発表日時と timezone のみ更新。
    ev["datetime_local"] = new_local
    ev["datetime_utc"] = new_utc
    ev["timezone"] = "Asia/Tokyo"


def fix_us_pce(ev: dict, changes: list[str]) -> None:
    """米PCE 1件の発表日時のみ修正する（真値表該当月）。id・title は変更しない。"""
    tgt_m = title_target_month(ev["title"])
    if tgt_m is None or tgt_m not in US_PCE_TRUTH_2026:
        # 真値表に無い月は触らない（過去月は優先度低）
        return
    new_date = US_PCE_TRUTH_2026[tgt_m]

    old_local = ev["datetime_local"]
    old_utc = ev["datetime_utc"]

    # 8:30 ET 固定。offset は夏冬で zoneinfo から決定
    offset = et_offset_str(new_date, 8, 30)
    new_local = f"{new_date.strftime('%Y-%m-%d')}T08:30:00{offset}"
    new_utc = to_utc_z(new_local)

    if (new_local, new_utc) != (old_local, old_utc):
        changes.append(
            f"  [US PCE] {ev['title']}  (id 据置: {ev['id']})\n"
            f"      local: {old_local} -> {new_local}\n"
            f"      utc  : {old_utc} -> {new_utc}"
        )
    # ★ id・title は触らない。発表日時のみ更新。
    ev["datetime_local"] = new_local
    ev["datetime_utc"] = new_utc


def fix_us_cpi(ev: dict, changes: list[str]) -> None:
    """米CPI 1件の発表日時のみ修正する（1月分の誤りのみ）。id・title は変更しない。"""
    tgt_m = title_target_month(ev["title"])

    # 真値表該当（1月分）以外は一切触らない（id を local日付に揃える整合は廃止）
    if tgt_m is None or tgt_m not in US_CPI_TRUTH_2026:
        return

    old_local = ev["datetime_local"]
    old_utc = ev["datetime_utc"]

    new_date = US_CPI_TRUTH_2026[tgt_m]
    offset = et_offset_str(new_date, 8, 30)
    new_local = f"{new_date.strftime('%Y-%m-%d')}T08:30:00{offset}"
    new_utc = to_utc_z(new_local)

    if (new_local, new_utc) != (old_local, old_utc):
        changes.append(
            f"  [US CPI] {ev['title']}  (id 据置: {ev['id']})\n"
            f"      local: {old_local} -> {new_local}\n"
            f"      utc  : {old_utc} -> {new_utc}"
        )
    # ★ id・title は触らない。発表日時のみ更新。
    ev["datetime_local"] = new_local
    ev["datetime_utc"] = new_utc


# ----------------------------------------------------------------------
# 検算
# ----------------------------------------------------------------------
def verify_local_utc_consistency(events: list[dict]) -> list[str]:
    """全件で local + offset == utc が成り立つか検算し、ズレのリストを返す。"""
    errors: list[str] = []
    for ev in events:
        local_iso = ev.get("datetime_local")
        utc_iso = ev.get("datetime_utc")
        if not local_iso or not utc_iso:
            continue
        try:
            computed_utc = to_utc_z(local_iso)
        except Exception as e:
            errors.append(f"{ev['id']}: local パース失敗 {local_iso} ({e})")
            continue
        if computed_utc != utc_iso:
            errors.append(
                f"{ev['id']}: local={local_iso} から計算した UTC={computed_utc} が "
                f"datetime_utc={utc_iso} と不一致"
            )
    return errors


# ----------------------------------------------------------------------
# メイン
# ----------------------------------------------------------------------
def main() -> int:
    data = load_canonical_events()
    events = data.get("events", [])
    if not events:
        log("シードに events がありません")
        return 1

    # 修正前の id 集合を控えておき、処理後に「id が一切変わっていない」ことを保証する
    ids_before = [ev["id"] for ev in events]

    changes: list[str] = []
    for ev in events:
        cat = ev.get("category")
        country = ev.get("country")
        if cat == "CPI" and country == "JP":
            fix_jp_cpi(ev, changes)
        elif cat == "PCE":
            fix_us_pce(ev, changes)
        elif cat == "CPI" and country == "US":
            fix_us_cpi(ev, changes)

    # ★ id 不変ガード：処理前後で id 列が完全一致していなければ即停止
    ids_after = [ev["id"] for ev in events]
    if ids_before != ids_after:
        diff = [(b, a) for b, a in zip(ids_before, ids_after) if b != a]
        log("★エラー: id が変更されています（ID不変ポリシー違反）:")
        for b, a in diff:
            log(f"  {b} -> {a}")
        return 1

    # id 重複チェック（念のため）
    seen: dict[str, int] = {}
    for ev in events:
        seen[ev["id"]] = seen.get(ev["id"], 0) + 1
    dups = [k for k, v in seen.items() if v > 1]
    if dups:
        log(f"★エラー: id が重複しています: {dups}")
        return 1

    # 検算: local + offset == utc
    errors = verify_local_utc_consistency(events)
    if errors:
        log("★検算エラー: local と utc が不整合のイベントがあります:")
        for e in errors:
            log(f"  {e}")
        return 1

    # events を datetime_utc 昇順に並べ直す（build_events と同じ並び）
    events.sort(key=lambda e: e["datetime_utc"])
    data["events"] = events

    # 書き込み
    write_canonical_events(data)

    # 変更ログ出力
    if changes:
        log(f"=== 修正したイベント {len(changes)} 件（id はすべて据え置き）===")
        for c in changes:
            log(c)
    else:
        log("変更なし（既に修正済み・冪等）")
    log(f"id 不変ガード OK（{len(events)} 件すべて id 不変）")
    log(f"検算 OK（全 {len(events)} 件で local+offset == utc を確認）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
