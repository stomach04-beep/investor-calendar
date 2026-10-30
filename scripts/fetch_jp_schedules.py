"""
日本の指標・市場日程を公式から取って tmp/fetch_jp_schedules_out.json に出す。

対象と出どころ:
  1. 日本GDP（四半期別GDP速報）… 内閣府 経済社会総合研究所「公表予定」HTML
  2. 日銀短観                    … 日本銀行「統計データの公表・掲載予定」xlsx
  3. 全国CPI                     … 総務省統計局「公表予定」XML（e-Stat の公表予定）
  4. 権利付き最終日・大発会・大納会 … 東証の休場日表 data/jp_market_holidays.json から計算

なぜ作ったか（2026-10-30）:
  これらはシード JSON の手入力しか無く、日本GDP・短観は全件「前日 23:50 JST」
  （正しくは 8:50）、12月短観は 4日ずれ、12月の権利付き最終日・大納会も1日ずれていた。
  全国CPIはルール計算を「確定」と名乗っていたため、外れても保護されて直らなかった。

確定と推定の決まり（このスクリプトの約束）:
  - その日に公式から取れた値だけを is_estimated=False にし、official_live=True を付ける。
    notion_upsert は official_live の付いた値なら既存の確定行も上書きする
    （人の手直しより、今日の公式発表を正とする）。
  - 公式が読めなかったとき・公式の掲載範囲の外は出さない（シードの値が残る）。
    読めなかったら record_fetch_warning で警告を残す（→ health-watchdog が LINE 通知）。
  - 時刻は規則表 release_rules から取る。公式に時刻が書いてあれば公式を使う。

id は変えない:
  既存のシードと同じ対象期間の行は、シードの id をそのまま使う（日付入りの id でも）。
  id を変えると Notion に重複ページができるため（feedback_upsert_id_immutable）。

使い方:
  python scripts/fetch_jp_schedules.py              # 本番（tmp に書く）
  python scripts/fetch_jp_schedules.py --self-test  # 取得結果を一覧表示
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_canonical_events, log, record_fetch_warning, write_tmp  # noqa: E402
from release_rules import (  # noqa: E402
    at_rule_time,
    is_jp_open,
    jp_calendar_known,
    jp_shift_open_days,
    rule_by_prefix,
)

ESRI_URL = "https://www.esri.cao.go.jp/jp/sna/kouhyou/kouhyou_top.html"
BOJ_XLSX_URL = "https://www.boj.or.jp/statistics/outline/tkohyos.xlsx"
BOJ_TANKAN_SOURCE_URL = "https://www.boj.or.jp/statistics/tk/"
STAT_CPI_XML_URL = "https://www.stat.go.jp/data/kouhyou/e-stat_cpi.xml"
JPX_CALENDAR_SOURCE_URL = "https://www.jpx.co.jp/corporate/about-jpx/calendar/index.html"

# 官公庁のサイトはブラウザ相当の名乗りで取る（既定の python-requests は弾かれることがある）
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 30


def http_get(url: str) -> bytes:
    """公式ページを取る（失敗は例外のまま呼び出し側へ）。"""
    r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content


def load_seed() -> list[dict]:
    """シード JSON のイベント一覧（読めなければ空）。"""
    try:
        return load_canonical_events().get("events", [])
    except Exception as e:
        log(f"  fetch_jp_schedules: シード読込失敗 ({type(e).__name__}: {e}) → id の引き継ぎなしで続行")
        return []


# ----------------------------------------------------------------------
# 1. 日本GDP（内閣府）
# ----------------------------------------------------------------------
GDP_KIND_LABEL = {"1st": "速報値", "2nd": "改定値"}   # 1次速報＝速報値・2次速報＝改定値（従来の題名に合わせる）
_ESRI_ROW = re.compile(
    r"(\d{4})年(\d{1,2})-(\d{1,2})月期（([12])次速報）\s*"
    r"(\d{4})（令和\d+）年(\d{1,2})月(\d{1,2})日（.）\s*(\d{1,2})時(\d{1,2})分"
)
_SEED_GDP_TITLE = re.compile(r"(?:(\d{4})年)?(\d{1,2})-(\d{1,2})月期GDP(速報値|改定値)")


def html_to_text(raw: bytes) -> str:
    """HTML から本文の文字だけを取り出す（コメント・script・style を先に落とす）。"""
    s = raw.decode("utf-8", errors="replace")
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s.replace("&nbsp;", " "))


def parse_esri_schedule(raw: bytes) -> list[tuple[int, int, str, date, int, int]]:
    """内閣府の公表予定から (対象年, 期の始まりの月, 1st/2nd, 公表日, 時, 分) を取り出す。

    「四半期別GDP速報」の表だけを読む（同じページの年次推計・資本ストックは対象外）。
    """
    text = html_to_text(raw)
    start = text.find("四半期別GDP速報")
    if start < 0:
        return []
    # 次の表（国民経済計算年次推計）の手前までに絞る
    end = text.find("国民経済計算年次推計", start)
    section = text[start:end if end > 0 else None]
    out = []
    for m in _ESRI_ROW.finditer(section):
        y, m1, _m2, n, py, pm, pd, hh, mm = m.groups()
        out.append((int(y), int(m1), "1st" if n == "1" else "2nd",
                    date(int(py), int(pm), int(pd)), int(hh), int(mm)))
    return out


def seed_gdp_index(seed: list[dict]) -> dict[tuple[int, int, str], dict]:
    """シードの日本GDPを (対象年, 期の始まりの月, 1st/2nd) → イベント の辞書にする。"""
    out = {}
    for ev in seed:
        if not str(ev.get("id", "")).startswith("jp_gdp_"):
            continue
        m = _SEED_GDP_TITLE.search(str(ev.get("title", "")))
        if not m:
            continue
        year = int(m.group(1)) if m.group(1) else int(ev["id"][7:11])
        kind = "1st" if m.group(4) == "速報値" else "2nd"
        out[(year, int(m.group(2)), kind)] = ev
    return out


def _gdp_title(year: int, m1: int, kind: str, pub: date) -> str:
    """題名。公表年と対象年が違う（10-12月期を翌年2月に出す）ときだけ年を付ける。"""
    period = f"{m1}-{m1 + 2}月期"
    if year != pub.year:
        period = f"{year}年{period}"
    return f"{period}GDP{GDP_KIND_LABEL[kind]}"


def build_jp_gdp(seed: list[dict], raw: bytes | None) -> list[dict]:
    """公式に載っている回は確定で、シードにしか無い回は時刻だけ直して推定で出す。"""
    rule = rule_by_prefix("jp_gdp_")
    index = seed_gdp_index(seed)
    events: list[dict] = []
    official: set[tuple[int, int, str]] = set()
    rows = parse_esri_schedule(raw) if raw else []
    for year, m1, kind, pub, hh, mm in rows:
        key = (year, m1, kind)
        official.add(key)
        base = index.get(key, {})
        local, utc = at_rule_time(rule, pub, hh, mm)
        events.append({
            "id": base.get("id") or f"jp_gdp_{year}q{(m1 + 2) // 3}_{kind}",
            "title": _gdp_title(year, m1, kind, pub),
            "category": "GDP",
            "country": "JP",
            "datetime_utc": utc,
            "datetime_local": local,
            "timezone": rule.tz,
            "importance": base.get("importance", 2),
            "is_estimated": False,
            "official_live": True,
            "description": base.get("description") or "内閣府 四半期別GDP速報。",
            "source_url": ESRI_URL,
            "result": base.get("result"),
        })
    # シードにしか無い回（過去分・公式の掲載範囲の外）は、id の日付の 8:50 に直す。
    # 二重がけで「前日 23:50」になっていた時刻を、規則表の時刻へ戻すだけ（日付は推定のまま）。
    for key, ev in index.items():
        if key in official:
            continue
        fixed = _repair_to_rule_time(ev, rule)
        if fixed is not None:
            events.append(fixed)
    log(f"  fetch_jp_schedules[GDP]: 内閣府から {len(rows)} 件・シードの時刻直し {len(events) - len(rows)} 件")
    return events


def _repair_to_rule_time(ev: dict, rule) -> dict | None:
    """シードの行を「id に書いた日付」×「規則の時刻」に直した推定行を返す。

    id の末尾が日付（YYYY-MM-DD）でなければ直さない（None）。
    確定の行（いったん公式から取った回）も直さない。公式の掲載から外れた後に
    id の日付（もとの推定日）へ戻すと、かえってずれるため。
    """
    if not ev.get("is_estimated", True):
        return None
    m = re.search(r"(\d{4}-\d{2}-\d{2})$", str(ev.get("id", "")))
    if not m:
        return None
    local, utc = at_rule_time(rule, date.fromisoformat(m.group(1)))
    out = dict(ev)
    out.update({"datetime_local": local, "datetime_utc": utc, "timezone": rule.tz,
                "is_estimated": True})
    out.pop("official_live", None)
    return out


# ----------------------------------------------------------------------
# 2. 日銀短観（日本銀行の公表予定 xlsx）
# ----------------------------------------------------------------------
# 旧シードの id は公表日入り。公表月 → 調査月（4月1日公表＝3月調査）で対象を割り出す
_TANKAN_PUB_TO_SURVEY = {4: 3, 7: 6, 10: 9, 12: 12}
_SURVEY_LABEL = re.compile(r"(\d{1,2})月調査")


def parse_boj_tankan(raw: bytes) -> list[tuple[int, int, date, int, int]]:
    """日銀の公表予定 xlsx から短観（概要及び要旨）の (調査年, 調査月, 公表日, 時, 分) を取り出す。

    表の形: 「短観…／概要及び要旨」の行が2行続き、1行目に時刻と「(12月調査)」、
    2行目に同じ列の公表日が入っている。列見出しの月は使わず、ラベルと日付を列ごとに組にする。
    """
    import openpyxl  # 重いので必要時だけ
    wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    out = []
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        for i in range(len(rows) - 1):
            name = " ".join(str(c) for c in rows[i][:3] if c is not None)
            if "短観" not in name or "概要及び要旨" not in name:
                continue
            hh, mm = _parse_hhmm(rows[i])
            labels, dates = rows[i], rows[i + 1]
            for col, label in enumerate(labels):
                m = _SURVEY_LABEL.search(str(label or ""))
                if not m or col >= len(dates):
                    continue
                pub = dates[col]
                if isinstance(pub, datetime):
                    pub = pub.date()
                if not isinstance(pub, date):
                    continue
                survey_month = int(m.group(1))
                # 12月調査は同じ年の12月公表、3月調査は4月公表（どちらも公表年＝調査年）
                out.append((pub.year, survey_month, pub, hh, mm))
            return out
    return out


def _parse_hhmm(row: list) -> tuple[int, int]:
    """行の中の「08:50:00」のような時刻を探す（無ければ規則表の時刻）。"""
    rule = rule_by_prefix("jp_tankan_")
    for c in row:
        if hasattr(c, "hour") and hasattr(c, "minute") and not isinstance(c, datetime):
            return c.hour, c.minute
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*", str(c or ""))
        if m:
            return int(m.group(1)), int(m.group(2))
    return rule.hour, rule.minute


def seed_tankan_index(seed: list[dict]) -> dict[tuple[int, int], dict]:
    """シードの短観を (調査年, 調査月) → イベント の辞書にする（公表月から調査月を割り出す）。"""
    out = {}
    for ev in seed:
        ev_id = str(ev.get("id", ""))
        m = re.fullmatch(r"jp_tankan_(\d{4})-(\d{2})(?:-(\d{2}))?", ev_id)
        if not m:
            continue
        year, month = int(m.group(1)), int(m.group(2))
        if m.group(3):   # 旧形式＝公表日入り
            month = _TANKAN_PUB_TO_SURVEY.get(month, month)
        out[(year, month)] = ev
    return out


def _tankan_title(survey_month: int) -> str:
    return f"日銀短観 ({survey_month}月調査)"


def build_jp_tankan(seed: list[dict], raw: bytes | None) -> list[dict]:
    """公式に載っている回は確定で、シードにしか無い回は時刻と題名だけ直して推定で出す。"""
    rule = rule_by_prefix("jp_tankan_")
    index = seed_tankan_index(seed)
    events: list[dict] = []
    rows = parse_boj_tankan(raw) if raw else []
    official = set()
    for year, survey_month, pub, hh, mm in rows:
        key = (year, survey_month)
        official.add(key)
        base = index.get(key, {})
        local, utc = at_rule_time(rule, pub, hh, mm)
        events.append({
            "id": base.get("id") or f"jp_tankan_{year}-{survey_month:02d}",
            "title": _tankan_title(survey_month),
            "category": "TANKAN",
            "country": "JP",
            "datetime_utc": utc,
            "datetime_local": local,
            "timezone": rule.tz,
            "importance": base.get("importance", 2),
            "is_estimated": False,
            "official_live": True,
            "description": base.get("description") or "日本銀行 全国企業短期経済観測調査。",
            "source_url": BOJ_TANKAN_SOURCE_URL,
            "result": base.get("result"),
        })
    for key, ev in index.items():
        if key in official:
            continue
        fixed = _repair_to_rule_time(ev, rule)
        if fixed is not None:
            fixed["title"] = _tankan_title(key[1])
            events.append(fixed)
    log(f"  fetch_jp_schedules[TANKAN]: 日銀から {len(rows)} 件・シードの時刻直し {len(events) - len(rows)} 件")
    return events


# ----------------------------------------------------------------------
# 3. 全国CPI（総務省の公表予定 XML）
# ----------------------------------------------------------------------
_CPI_MONTH = re.compile(r"^(\d{4})年(\d{1,2})月分$")


def parse_stat_cpi(raw: bytes) -> list[tuple[int, int, date, int, int]]:
    """総務省の公表予定 XML から全国の月次分の (対象年, 対象月, 公表日, 時, 分) を取り出す。

    「2025年平均」「遡及結果」などの月次でない行と、東京都区部の行は読まない。
    """
    root = ET.fromstring(raw)
    out = []
    for c1 in root.iter("class_1"):
        if c1.get("name") != "全国":
            continue
        for c2 in c1.findall("class_2"):
            m = _CPI_MONTH.match((c2.get("name") or "").strip())
            if not m:
                continue
            c5 = next(c2.iter("class_5"), None)
            if c5 is None:
                continue
            try:
                pub = date(int(c5.findtext("release_year")), int(c5.findtext("release_month")),
                           int(c5.findtext("release_day")))
                hh = int(c5.findtext("release_hour"))
                mm = int(c5.findtext("release_minute"))
            except (TypeError, ValueError):
                continue
            out.append((int(m.group(1)), int(m.group(2)), pub, hh, mm))
    return out


def build_jp_cpi_official(raw: bytes | None) -> list[dict]:
    """公表予定に載っている月を確定値で出す（載っていない月は fetch_schedules のルール計算が残る）。"""
    from fetch_schedules import load_seed_id_alias, make_jp_cpi_event  # 題名・id の作り方を共有する
    alias = load_seed_id_alias()
    events = []
    for tgt_y, tgt_m, pub, hh, mm in (parse_stat_cpi(raw) if raw else []):
        ev = make_jp_cpi_event(tgt_y, tgt_m, pub, alias, is_estimated=False, hour=hh, minute=mm)
        ev["official_live"] = True
        ev["source_url"] = STAT_CPI_XML_URL
        events.append(ev)
    log(f"  fetch_jp_schedules[CPI]: 総務省から {len(events)} 件")
    return events


# ----------------------------------------------------------------------
# 4. 権利付き最終日・大発会・大納会（東証の休場日表から計算）
# ----------------------------------------------------------------------
EXDIV_MONTHS = (3, 6, 9, 12)   # 配当・優待の多い月末（従来のシードと同じ4回）


def exdiv_last_day(year: int, month: int) -> date:
    """月末権利の権利付き最終日。

    権利確定日＝月末の最後の営業日（12月は大納会）。その2営業日前が権利付き最終日
    （受け渡しが約定日から2営業日後のため）。例: 2026年12月 → 12/30(水) の2営業日前＝12/28(月)。
    """
    d = date(year + (month == 12), month % 12 + 1, 1)
    d = jp_shift_open_days(d, -1)   # 月末の最後の営業日
    return jp_shift_open_days(d, -2)


def first_open_day(year: int) -> date:
    """大発会＝その年の最初の営業日。"""
    return jp_shift_open_days(date(year - 1, 12, 31), 1)


def last_open_day(year: int) -> date:
    """大納会＝その年の最後の営業日。"""
    return jp_shift_open_days(date(year + 1, 1, 1), -1)


def _seed_market_index(seed: list[dict]) -> dict[tuple[str, int, int], dict]:
    """シードの権利付き最終日・大発会・大納会を (種類, 年, 月) → イベント にする。"""
    out = {}
    for ev in seed:
        ev_id = str(ev.get("id", ""))
        m = re.search(r"(\d{4})-(\d{2})", ev_id)
        if not m:
            continue
        y, mo = int(m.group(1)), int(m.group(2))
        title = str(ev.get("title", ""))
        if ev_id.startswith("jp_exdiv_"):
            out[("exdiv", y, mo)] = ev
        elif ev_id.startswith("jp_market_") and "大発会" in title:
            out[("okatsu", y, 1)] = ev
        elif ev_id.startswith("jp_market_") and "大納会" in title:
            out[("onoukai", y, 12)] = ev
    return out


def build_jp_market_calendar(seed: list[dict], years: list[int]) -> list[dict]:
    """休場日表から計算する。表の範囲内なら確定（official_live）、範囲外は推定。"""
    index = _seed_market_index(seed)
    plan = []
    for y in years:
        for mo in EXDIV_MONTHS:
            plan.append(("exdiv", y, mo, exdiv_last_day(y, mo), "jp_exdiv_",
                         f"{mo}月度 権利付き最終日", 1, f"jp_exdiv_{y}-{mo:02d}"))
        plan.append(("okatsu", y, 1, first_open_day(y), "jp_market_okatsu_",
                     "大発会", 1, f"jp_market_okatsu_{y}"))
        plan.append(("onoukai", y, 12, last_open_day(y), "jp_market_onoukai_",
                     "大納会", 1, f"jp_market_onoukai_{y}"))
    events = []
    for kind, y, mo, d, prefix, title, importance, new_id in plan:
        rule = rule_by_prefix(prefix)
        base = index.get((kind, y, mo), {})
        known = jp_calendar_known(d)
        local, utc = at_rule_time(rule, d)
        ev = {
            "id": base.get("id") or new_id,
            "title": title,
            "category": base.get("category") or ("DIVIDEND" if kind == "exdiv" else "MARKET"),
            "country": "JP",
            "datetime_utc": utc,
            "datetime_local": local,
            "timezone": rule.tz,
            "importance": base.get("importance", importance),
            "is_estimated": not known,
            "description": base.get("description") or "東証の営業日カレンダーから計算。",
            "source_url": base.get("source_url") or JPX_CALENDAR_SOURCE_URL,
            "result": base.get("result"),
        }
        if known:
            ev["official_live"] = True
        events.append(ev)
    log(f"  fetch_jp_schedules[市場日程]: {len(events)} 件（{years}）")
    return events


# ----------------------------------------------------------------------
# 本体
# ----------------------------------------------------------------------
def _fetch_or_warn(label: str, url: str) -> bytes | None:
    """公式を取る。取れなければ警告を残して None（その系統はシードの値のまま）。"""
    try:
        return http_get(url)
    except Exception as e:
        log(f"  fetch_jp_schedules[{label}]: 取得失敗 ({type(e).__name__}: {e})")
        record_fetch_warning(f"fetch_jp_schedules[{label}]",
                             f"公式から取得できずシードの値のまま（{type(e).__name__}）")
        return None


def collect(today: date | None = None) -> list[dict]:
    """4系統を独立に作ってまとめる（1系統の失敗で他を止めない）。"""
    today = today or datetime.now(timezone.utc).date()
    seed = load_seed()
    events: list[dict] = []
    jobs = [
        ("GDP", ESRI_URL, lambda raw: build_jp_gdp(seed, raw)),
        ("TANKAN", BOJ_XLSX_URL, lambda raw: build_jp_tankan(seed, raw)),
        ("CPI", STAT_CPI_XML_URL, build_jp_cpi_official),
    ]
    for label, url, build in jobs:
        raw = _fetch_or_warn(label, url)
        try:
            built = build(raw)
        except Exception as e:
            log(f"  fetch_jp_schedules[{label}]: 解析失敗 ({type(e).__name__}: {e})")
            record_fetch_warning(f"fetch_jp_schedules[{label}]", f"公式の形式を読めず（{type(e).__name__}）")
            continue
        # ページは取れたのに1件も読めない＝ページの形が変わった合図。緑で素通りさせない
        if raw is not None and not any(e.get("official_live") for e in built):
            record_fetch_warning(f"fetch_jp_schedules[{label}]", "公式ページは取れたが日程を1件も読めず")
        events.extend(built)
    try:
        events.extend(build_jp_market_calendar(seed, [today.year, today.year + 1]))
    except Exception as e:
        log(f"  fetch_jp_schedules[市場日程]: 計算失敗 ({type(e).__name__}: {e})")
        record_fetch_warning("fetch_jp_schedules[市場日程]", f"計算失敗（{type(e).__name__}）")
    by_id: dict[str, dict] = {}
    for ev in events:
        by_id[ev["id"]] = ev   # 同じ id は後勝ち
    return sorted(by_id.values(), key=lambda e: e["datetime_utc"])


def main() -> int:
    parser = argparse.ArgumentParser(description="日本の指標・市場日程を公式から取得して tmp に出力")
    parser.add_argument("--self-test", action="store_true", help="取得結果を一覧表示する（tmp にも書く）")
    args = parser.parse_args()

    events = collect()
    path = write_tmp("fetch_jp_schedules_out", {
        "events": events,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    official = sum(1 for e in events if e.get("official_live"))
    log(f"fetch_jp_schedules: 全 {len(events)} 件（公式・確定 {official} 件）を {path} に書き出し")
    if args.self_test:
        for e in events:
            mark = "確定" if not e["is_estimated"] else "推定"
            log(f"    {e['id']} | {e['datetime_local']} | {mark} | {e['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
