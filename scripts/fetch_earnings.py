# -*- coding: utf-8 -*-
"""
保有株の次回決算日を取得して:
  (A) ポートフォリオ管理DB(Notion)の「次回決算日」プロパティを更新
  (B) 投資家カレンダー用の「決算」イベントを tmp/fetch_earnings_out.json に出力
する。

データ源:
  - JPX 決算発表予定Excel（日本株の第一候補）: 取引所公式の発表予定＝確定扱い(is_estimated=false)。
    ただし直近約1ヶ月分しか掲載されない
  - yfinance（米国株の主力・日本株のJPX圏外フォールバック）: Ticker.calendar["Earnings Date"]（日付のみ採用）
  - J-Quants 予測日 data/jq_earnings_jp.json（日本株の最終フォールバック）:
    jquants-bulk/build_earnings_estimates.py が10年開示履歴から生成した
    「昨年同四半期の実開示日+364日」の予測。JPX予定Excelは直近約1ヶ月分しか
    無いため、決算通過後〜次回掲載までの空白期間もイベントが消えないようにする

役割分担（既存パイプラインの思想を踏襲）:
  - 投資家カレンダーDB への「決算」イベント登録は notion_upsert.py が
    tmp/fetch_earnings_out.json をマージして行う（本スクリプトはイベントを生成するだけ）。
  - ただし「ポートフォリオDBの次回決算日更新」と「売却済み銘柄の決算イベント掃除」は
    保有状態を知っている本スクリプトが直接 Notion に書く。

モード:
  通常         : Notion 読み書き（NOTION_TOKEN 必須）
  --dry-run    : Notion を読むが書かない（件数・ログのみ）
  --self-test  : Notion 不要。サンプル保有株で yfinance/JPX 取得とイベント生成のみ確認

実行:
  set NOTION_TOKEN=secret_xxx
  python scripts/fetch_earnings.py
  python scripts/fetch_earnings.py --dry-run
  python scripts/fetch_earnings.py --self-test
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    NotionClient,
    get_notion_db_id,
    log,
    read_date_start,
    read_rich_text,
    read_select,
    read_title,
    write_tmp,
)

# ----------------------------------------------------------------------
# 定数
# ----------------------------------------------------------------------
UA = "investor-calendar-bot/1.0 (+https://github.com/stomach04-beep/investor-calendar)"

# 「保有中」とみなすステータス（これ以外＝未購入/売却予定/売却済 は対象外）
HELD_STATUSES = {"保有継続", "目標達成", "部分達成", "打診買い済"}

TZ_JST = ZoneInfo("Asia/Tokyo")
TZ_ET = ZoneInfo("America/New_York")

# 日本株のデフォルト決算発表時刻（15:00 JST 想定）。
# 米国株は「寄り前(BMO)＝07:00 ET」「引け後(AMC)・不明＝16:00 ET」で出し分ける。
#   yfinance の時刻は分単位では不正確だが、寄り前/引け後の別(AM 朝 or PM 16:00)は信頼できる。
#   実証: AAPL/MSFT/NVDA=16:00(引け後), GS/JPM/KO=朝(寄り前), VZ/CVX=08:00(寄り前)。
#   → セッション(AM/PM)だけ採用し、代表時刻に丸めて表示・通知する（VZ実際の7:00press/8:30会見に近い）。
JP_HOUR, JP_MIN = 15, 0
US_HOUR, US_MIN = 16, 0          # 引け後(AMC)・セッション不明時のデフォルト
US_BMO_HOUR, US_BMO_MIN = 7, 0   # 寄り前(BMO)の代表時刻（プレスリリース想定）

# self-test 用サンプル（Notion 不要でデータ取得を確認するための固定リスト）
SELF_TEST_HOLDINGS = [
    {"name": "NVIDIA", "ticker": "NVDA", "market": "米国", "page_id": None, "current": None, "date_prop": "次回決算日"},
    {"name": "Apple", "ticker": "AAPL", "market": "米国", "page_id": None, "current": None, "date_prop": "次回決算日"},
    {"name": "任天堂", "ticker": "7974", "market": "日本", "page_id": None, "current": None, "date_prop": "次回決算日"},
    {"name": "三菱UFJ", "ticker": "8306", "market": "日本", "page_id": None, "current": None, "date_prop": "次回決算日"},
]

# ウォッチ銘柄（保有していないが決算日をカレンダーに載せたい銘柄）。
# ここに1行追記すれば、毎朝 yfinance から次回決算日を自動取得してカレンダーへ反映される。
#   name   … カレンダーに出す表示名（「○○ 決算」になる）
#   ticker … 米国株はティッカー（例 CVX）、日本株は4桁コード（例 7203）
#   market … "米国" か "日本"
#   is_watch=True … 保有株(hold_earnings_*)と区別し watch_earnings_* の id にするための目印
# 保有していないだけなので、ポートフォリオDBの「次回決算日」更新の対象にはしない（page_id=None）。
# ※保有株になった銘柄をここに残しても二重登録にはならない（main で自動的に抑止する）。
WATCH_HOLDINGS = [
    {"name": "ファクトセット", "ticker": "FDS", "market": "米国",
     "page_id": None, "current": None, "date_prop": "次回決算日", "is_watch": True},
    {"name": "ローパーテクノロジーズ", "ticker": "ROP", "market": "米国",
     "page_id": None, "current": None, "date_prop": "次回決算日", "is_watch": True},
    # --- 2026-07-25 追加: 米国配当成長株スクリーニングの買い候補（未保有） ---
    {"name": "アメックス", "ticker": "AXP", "market": "米国",
     "page_id": None, "current": None, "date_prop": "次回決算日", "is_watch": True},
    {"name": "ムーディーズ", "ticker": "MCO", "market": "米国",
     "page_id": None, "current": None, "date_prop": "次回決算日", "is_watch": True},
    {"name": "マスターカード", "ticker": "MA", "market": "米国",
     "page_id": None, "current": None, "date_prop": "次回決算日", "is_watch": True},
    {"name": "ノースロップグラマン", "ticker": "NOC", "market": "米国",
     "page_id": None, "current": None, "date_prop": "次回決算日", "is_watch": True},
    {"name": "ロッキードマーチン", "ticker": "LMT", "market": "米国",
     "page_id": None, "current": None, "date_prop": "次回決算日", "is_watch": True},
    {"name": "キューリグドクターペッパー", "ticker": "KDP", "market": "米国",
     "page_id": None, "current": None, "date_prop": "次回決算日", "is_watch": True},
    {"name": "ヤムブランズ", "ticker": "YUM", "market": "米国",
     "page_id": None, "current": None, "date_prop": "次回決算日", "is_watch": True},
    {"name": "ホームデポ", "ticker": "HD", "market": "米国",
     "page_id": None, "current": None, "date_prop": "次回決算日", "is_watch": True},
    {"name": "ダラーゼネラル", "ticker": "DG", "market": "米国",
     "page_id": None, "current": None, "date_prop": "次回決算日", "is_watch": True},
]


def get_portfolio_db_id() -> str:
    """ポートフォリオ管理DBの database_id。env 優先、無ければ既定値。"""
    v = (os.environ.get("PORTFOLIO_DB_ID") or "").lstrip("﻿").strip()
    return v or "f724f7b77ca34d0fbbdaafe81003d956"


# ----------------------------------------------------------------------
# ティッカー正規化
# ----------------------------------------------------------------------
def to_yf_symbol(ticker: str, market: str) -> str:
    """yfinance 用シンボルに整形。日本株は『4桁コード + .T』。"""
    t = (ticker or "").strip().upper()
    if market == "日本":
        code = t.replace(".T", "").strip()
        return f"{code}.T" if code else ""
    return t  # 米国株はティッカーそのまま


def to_jp_code(ticker: str) -> str:
    """JPX 照合用の銘柄コード（.T を除いた4桁）。"""
    return (ticker or "").strip().upper().replace(".T", "")


def is_valid_ticker(ticker: str, market: str) -> bool:
    """
    yfinance で扱えるティッカー形式かを判定する。
    投資信託・債券（例『DC米国株IDX』『Tボンド』）は日本語コードなので除外される。
      - 日本株: 4桁数字（.T は許容）
      - 米国株: 先頭が英字で、英数字・ピリオド・ハイフンのみ（例 NVDA, BRK.B）
    """
    t = (ticker or "").strip().upper()
    if market == "日本":
        return bool(re.fullmatch(r"\d{4}", t.replace(".T", "")))
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]*", t))


# ----------------------------------------------------------------------
# 時刻ユーティリティ
# ----------------------------------------------------------------------
def et_offset_str(d: date, hour: int, minute: int) -> str:
    """指定日の ET(America/New_York) の UTC オフセット文字列（例 '-04:00'）。"""
    aware = datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ_ET)
    off = aware.utcoffset()
    assert off is not None
    total = int(off.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 60:02d}:{total % 60:02d}"


def to_utc_z(local_iso: str) -> str:
    """オフセット付き ISO 文字列を UTC の 'YYYY-MM-DDTHH:MM:SSZ' に変換。"""
    aware = datetime.fromisoformat(local_iso)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------
# yfinance 取得（主力）
# ----------------------------------------------------------------------
def _yf_session_for(tk, edate: date) -> str | None:
    """yfinance の get_earnings_dates から、指定日の発表セッションを返す。
    'AM'（寄り前 BMO）／'PM'（引け後 AMC）／None（不明）。
    yfinance の時刻は分単位では不正確だが AM/PM の別は信頼できる（実証済）。

    注意: calendar の日付と get_earnings_dates の日付は、引け後(AMC)銘柄で
    1日ズレることがある（例 AAPL: calendar=7/31 / earnings_dates=7/30 16:00）。
    そのため edate と完全一致でなく ±1日の最も近い行を採用する。
    時刻が 00:00（プレースホルダ＝時刻不明）の行は不明扱い。"""
    try:
        df = tk.get_earnings_dates(limit=16)
        if df is None or len(df) == 0:
            return None
        best_ix = None
        best_diff = 2  # ±1日以内のみ採用
        for ix in df.index:
            # ix は America/New_York の tz-aware Timestamp。ET の日付・時で判定する。
            diff = abs((ix.date() - edate).days)
            if diff < best_diff:
                best_diff, best_ix = diff, ix
        if best_ix is None:
            return None
        if best_ix.hour == 0 and best_ix.minute == 0:
            return None  # 時刻不明のプレースホルダ
        return "AM" if best_ix.hour < 12 else "PM"
    except Exception:
        return None


def yf_next_earnings(symbol: str, want_session: bool = False) -> tuple[date | None, str | None]:
    """yfinance で次回決算予定日(date)と発表セッション('AM'/'PM'/None)を返す。
    取れなければ (None, None)。want_session=False のときはセッション判定を省く
    （日本株は時刻を使わないので余計な API 呼び出しを避ける）。"""
    if not symbol:
        return None, None
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        cal = tk.calendar
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not ed:
            return None, None
        if isinstance(ed, (list, tuple)):
            ed = ed[0] if ed else None
        edate: date | None = None
        if isinstance(ed, datetime):
            edate = ed.date()
        elif isinstance(ed, date):
            edate = ed
        else:
            try:
                edate = date.fromisoformat(str(ed)[:10])
            except ValueError:
                return None, None
        if edate is None:
            return None, None
        session = _yf_session_for(tk, edate) if want_session else None
        return edate, session
    except Exception as e:
        log(f"  yfinance {symbol} 取得失敗: {type(e).__name__}: {e}")
        return None, None


# ----------------------------------------------------------------------
# JPX 決算発表予定Excel（日本株の補完）
# ----------------------------------------------------------------------
JPX_INDEX = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
JPX_BASE = "https://www.jpx.co.jp"


def jpx_earnings_map() -> dict[str, date]:
    """
    JPX の決算発表予定Excelを全て読み、{4桁コード: 決算発表予定日} を返す。
    ベストエフォート（失敗時は空 dict）。yfinance で取れない日本株の補完に使う。

    Excel 仕様（2026-06 時点で確認）:
      行5(0始まり4)=ヘッダ、データは行6から。列0=決算発表予定日, 列1=コード。
    """
    out: dict[str, date] = {}
    try:
        from bs4 import BeautifulSoup
        import openpyxl
        r = requests.get(JPX_INDEX, headers={"User-Agent": UA}, timeout=30)
        if r.status_code != 200:
            log(f"  JPX index HTTP {r.status_code} → 日本株補完なしで継続")
            return out
        soup = BeautifulSoup(r.text, "html.parser")
        hrefs: list[str] = []
        for a in soup.find_all("a"):
            h = a.get("href") or ""
            if h.lower().endswith(".xlsx") and "kessan" in h.lower():
                hrefs.append(h if h.startswith("http") else JPX_BASE + h)
        for url in hrefs:
            try:
                rr = requests.get(url, headers={"User-Agent": UA}, timeout=30)
                if rr.status_code != 200:
                    continue
                wb = openpyxl.load_workbook(io.BytesIO(rr.content), read_only=True, data_only=True)
                ws = wb.active
                for row in ws.iter_rows(min_row=6, values_only=True):
                    if not row or len(row) < 2:
                        continue
                    d, code = row[0], row[1]
                    if d is None or code is None:
                        continue
                    cd = str(code).strip().replace(".0", "")
                    if isinstance(d, datetime):
                        dd = d.date()
                    elif isinstance(d, date):
                        dd = d
                    else:
                        continue
                    # 同一コードは「より新しい予定日」を採用（更新版優先）
                    if cd not in out or dd > out[cd]:
                        out[cd] = dd
            except Exception as e:
                log(f"  JPX excel {url} 失敗: {type(e).__name__}")
                continue
        log(f"  JPX 補完マップ {len(out)} 件")
    except Exception as e:
        log(f"  JPX 取得失敗: {type(e).__name__}: {e} → 日本株補完なしで継続")
    return out


# ----------------------------------------------------------------------
# J-Quants 予測日（日本株の最終フォールバック）
# ----------------------------------------------------------------------
JQ_ESTIMATES_PATH = Path(__file__).resolve().parents[1] / "data" / "jq_earnings_jp.json"


def jq_estimates_map(today: date | None = None) -> dict[str, date]:
    """
    data/jq_earnings_jp.json（J-Quants 開示履歴からの予測日）を読み、
    {4桁コード: 今日以降で最も近い予測日} を返す。ベストエフォート（無ければ空 dict）。
    予測は「昨年同四半期の実開示日+364日」なので±数日ズレうる（is_estimated=true 前提）。
    """
    out: dict[str, date] = {}
    today = today or date.today()
    try:
        payload = json.loads(JQ_ESTIMATES_PATH.read_text(encoding="utf-8"))
        for code, dates in payload.get("estimates", {}).items():
            future = sorted(d for d in dates if d >= today.isoformat())
            if future:
                out[code] = date.fromisoformat(future[0])
        log(f"  J-Quants 予測マップ {len(out)} 件 (generated_at={payload.get('generated_at')})")
    except FileNotFoundError:
        log("  J-Quants 予測ファイルなし → フォールバックなしで継続")
    except Exception as e:
        log(f"  J-Quants 予測読込失敗: {type(e).__name__}: {e} → フォールバックなしで継続")
    return out


# ----------------------------------------------------------------------
# イベント生成
# ----------------------------------------------------------------------
def build_event(h: dict, edate: date) -> dict:
    """保有株/ウォッチ銘柄1件 + 決算日 から 投資家カレンダー用イベント dict を作る。"""
    market = h["market"]
    name = h["name"]
    ticker = h["ticker"]
    # JPX（取引所公式の発表予定）由来の日付は確定扱い＝is_estimated=false にする。
    # yfinance / J-Quants予測 由来は従来どおり推定扱い（毎朝の自動更新で追従させる）。
    confirmed = h.get("src") == "JPX"
    # ウォッチ銘柄（保有外）は id を watch_earnings_* にして保有株と区別する
    prefix = "watch_earnings" if h.get("is_watch") else "hold_earnings"
    kind = "ウォッチ銘柄" if h.get("is_watch") else "保有株"
    if market == "日本":
        country = "JP"
        code = to_jp_code(ticker)
        # id は銘柄ごとに固定（日付を含めない＝決算日が動いても upsert で更新・重複しない）
        ev_id = f"{prefix}_jp_{code}"
        local = f"{edate.isoformat()}T{JP_HOUR:02d}:{JP_MIN:02d}:00+09:00"
        tz = "Asia/Tokyo"
        src = f"https://finance.yahoo.co.jp/quote/{code}.T"
    else:
        country = "US"
        sym = ticker.strip().upper()
        ev_id = f"{prefix}_us_{sym}"
        # 寄り前(AM/BMO)=07:00 ET、引け後(PM/AMC)・不明=16:00 ET で出し分け
        if h.get("session") == "AM":
            hh, mm = US_BMO_HOUR, US_BMO_MIN
        else:
            hh, mm = US_HOUR, US_MIN
        off = et_offset_str(edate, hh, mm)
        local = f"{edate.isoformat()}T{hh:02d}:{mm:02d}:00{off}"
        tz = "America/New_York"
        src = f"https://finance.yahoo.com/quote/{sym}"
    return {
        "id": ev_id,
        "title": f"{name} 決算",
        "category": "EARNINGS",
        "country": country,
        "datetime_utc": to_utc_z(local),
        "datetime_local": local,
        "timezone": tz,
        "importance": 2,
        # JPX公式由来のみ確定（false）。それ以外は推定（true）で毎朝追従させる。
        # 確定行の更新可否は notion_upsert 側の決算イベント専用ルールで制御する。
        "is_estimated": not confirmed,
        "description": (
            f"{kind}の決算発表予定（{name}）。JPX公式の発表予定日。"
            if confirmed
            else f"{kind}の決算発表予定（{name}）。予定日は変更される場合があります。"
        ),
        "source_url": src,
        "result": None,
    }


# ----------------------------------------------------------------------
# Notion: 保有株の取得
# ----------------------------------------------------------------------
def _resolve_prop(props: dict, jp_name: str, expected_type: str) -> str:
    """日本語プロパティ名を型一致で解決（前後空白の揺れに耐える）。"""
    if jp_name in props and props[jp_name].get("type") == expected_type:
        return jp_name
    for name, meta in props.items():
        if name.strip() == jp_name and meta.get("type") == expected_type:
            return name
    return jp_name  # 見つからなくても名前で素直に試す


def fetch_holdings(client: NotionClient, db_id: str) -> list[dict]:
    """ポートフォリオDBから『保有中・ティッカーあり・ETF以外』の銘柄を抽出する。"""
    schema = client._request("GET", f"https://api.notion.com/v1/databases/{db_id}", None)
    props = schema.get("properties", {})
    p_ticker = _resolve_prop(props, "ティッカー", "rich_text")
    p_market = _resolve_prop(props, "市場", "select")
    p_status = _resolve_prop(props, "ステータス", "select")
    p_name = _resolve_prop(props, "銘柄名", "title")
    p_date = _resolve_prop(props, "次回決算日", "date")
    p_shares = _resolve_prop(props, "保有株数", "number")

    holds: list[dict] = []
    for pg in client.query_database(db_id):
        pr = pg.get("properties", {})
        status = read_select(pr.get(p_status, {}))
        if status not in HELD_STATUSES:
            continue
        # 保有株数0（持株会の積立開始直後など、まだ実保有がない）は決算対象外
        shares = pr.get(p_shares, {}).get("number")
        if shares is not None and shares <= 0:
            log(f"  skip(0株): {read_title(pr.get(p_name, {}))}")
            continue
        name = read_title(pr.get(p_name, {}))
        # 重複・無効・統合済みの整理用ページは決算対象外（銘柄名で判定）
        if any(ng in name for ng in ("【重複", "[重複", "無効】", "統合")):
            log(f"  skip(重複・無効): {name}")
            continue
        ticker = read_rich_text(pr.get(p_ticker, {})).strip()
        market = read_select(pr.get(p_market, {}))
        if not ticker or market == "ETF" or not market:
            continue
        # 投信・債券など yfinance で扱えない日本語コードを除外
        if not is_valid_ticker(ticker, market):
            log(f"  skip(非対応ティッカー): {name}({ticker})")
            continue
        holds.append({
            "name": name or ticker,
            "ticker": ticker,
            "market": market,
            "page_id": pg["id"],
            "current": read_date_start(pr.get(p_date, {})),
            "date_prop": p_date,
        })
    return holds


def update_portfolio_date(client: NotionClient, h: dict, edate: date, dry: bool) -> bool:
    """ポートフォリオDBの『次回決算日』を更新する（変化がある時だけ）。更新したら True。"""
    # ウォッチ銘柄などポートフォリオDBに行が無い銘柄（page_id なし）は更新対象外
    if not h.get("page_id"):
        return False
    cur = (h.get("current") or "")[:10]
    new = edate.isoformat()
    if cur == new:
        return False
    if not dry:
        client.update_page(h["page_id"], {h["date_prop"]: {"date": {"start": new}}})
    return True


def archive_stale(client: NotionClient, cal_db_id: str, current_ids: set[str], dry: bool) -> int:
    """
    投資家カレンダーDBの hold_earnings_*/watch_earnings_* イベントのうち、今回の
    保有・ウォッチセットに無いもの（＝売却された／ウォッチ対象から外した銘柄）を
    archive（アーカイブ）して掃除する。archive 件数を返す。
    """
    schema = client._request("GET", f"https://api.notion.com/v1/databases/{cal_db_id}", None)
    props = schema.get("properties", {})
    id_name = "ID"
    if id_name not in props:
        for n in props:
            if n.lower() == "id" or n.endswith(":ID") or n.endswith(":id"):
                id_name = n
                break
    n = 0
    for pg in client.query_database(cal_db_id):
        idv = read_rich_text(pg.get("properties", {}).get(id_name, {}))
        if idv.startswith(("hold_earnings_", "watch_earnings_")) and idv not in current_ids:
            if not dry:
                client.archive_page(pg["id"])
            n += 1
            log(f"  掃除(archive): {idv}")
    return n


# ----------------------------------------------------------------------
# メイン
# ----------------------------------------------------------------------
def _resolve_dates(holds: list[dict]) -> tuple[list[dict], list[dict]]:
    """各保有株の決算日を解決する。
      日本株: JPX公式（確定・約1ヶ月先まで）→ yfinance → J-Quants予測 の順
      米国株: yfinance のみ
    JPX は取引所の公式発表予定なので is_estimated=false（確定）として扱える。
    (date付きリスト, 取得失敗リスト) を返す。"""
    jpx: dict[str, date] = {}
    jq: dict[str, date] = {}
    if any(h["market"] == "日本" for h in holds):
        jpx = jpx_earnings_map()
        jq = jq_estimates_map()
    # JPXのExcelには発表直後の過去日が残っていることがあるため「今日(JST)以降」だけ採用する
    today_jst = datetime.now(TZ_JST).date()
    resolved: list[dict] = []
    missing: list[dict] = []
    for h in holds:
        sym = to_yf_symbol(h["ticker"], h["market"])
        # 米国株のみセッション(寄り前/引け後)を判定する（日本株は時刻を使わない）
        want_session = h["market"] != "日本"
        ed: date | None = None
        session: str | None = None
        src = ""
        # 日本株はまずJPX公式（確定日）を見る
        if h["market"] == "日本":
            jpx_d = jpx.get(to_jp_code(h["ticker"]))
            if jpx_d is not None and jpx_d >= today_jst:
                ed, src = jpx_d, "JPX"
        if ed is None:
            ed, session = yf_next_earnings(sym, want_session=want_session)
            src = "yfinance"
        if ed is None and h["market"] == "日本":
            # 最終フォールバック: J-Quants 開示履歴からの予測日（±数日ズレうる）
            ed = jq.get(to_jp_code(h["ticker"]))
            src = "JQ予測"
        if ed is None:
            missing.append(h)
            log(f"  {h['name']}({h['ticker']}): 決算日が取得できず（スキップ）")
            continue
        h2 = dict(h)
        h2["edate"] = ed
        h2["src"] = src
        h2["session"] = session  # 'AM'/'PM'/None（build_event で米国株の時刻に反映）
        resolved.append(h2)
        sess_label = f" [{session}]" if session else ""
        log(f"  {h['name']}({h['ticker']}) -> {ed} [{src}]{sess_label}")
    return resolved, missing


def main() -> int:
    ap = argparse.ArgumentParser(description="保有株の次回決算日を取得しカレンダーへ反映")
    ap.add_argument("--dry-run", action="store_true", help="Notion を読むが書き込まない")
    ap.add_argument("--self-test", action="store_true", help="Notion 不要・サンプル銘柄で取得と生成のみ確認")
    args = ap.parse_args()

    # ---- self-test: Notion を使わずデータ取得＋生成だけ確認 ----
    if args.self_test:
        # ウォッチ銘柄（CVX/VZ等）も含めて取得確認する
        resolved, missing = _resolve_dates(SELF_TEST_HOLDINGS + WATCH_HOLDINGS)
        events = [build_event(h, h["edate"]) for h in resolved]
        write_tmp("fetch_earnings_out", {
            "events": events,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        log(f"self-test 完了: 生成 {len(events)} 件 / 取得失敗 {len(missing)} 件")
        for e in events:
            log(f"    {e['id']} | {e['datetime_local']} | {e['title']}")
        return 0

    # ---- 通常 / dry-run ----
    client = NotionClient()
    pdb = get_portfolio_db_id()
    holds = fetch_holdings(client, pdb)
    log(f"  保有株（対象）{len(holds)} 件")
    # 保有はしていないが決算日を載せたいウォッチ銘柄を合流（FDS/ROP 等）
    # ★二重登録ガード: ウォッチ銘柄を実際に買うと、ポートフォリオDB経由の
    #   hold_earnings_* と watch_earnings_* が両方できてカレンダーに2件並ぶ。
    #   （VZ=2026-07-19・CVX=2026-07-25 に実際に発生）。保有済みティッカーは
    #   ここで落とし、残った watch_earnings_* は archive_stale が自動で掃除する。
    held_tickers = {(h.get("ticker") or "").upper() for h in holds}
    watch = [w for w in WATCH_HOLDINGS
             if (w.get("ticker") or "").upper() not in held_tickers]
    dropped = [w["ticker"] for w in WATCH_HOLDINGS if w not in watch]
    if dropped:
        log(f"  ウォッチ銘柄のうち保有済みのため除外: {', '.join(dropped)}")
    holds.extend(watch)
    log(f"  ＋ウォッチ銘柄 {len(watch)} 件 → 合計 {len(holds)} 件")
    if not holds:
        log("  対象保有株なし → 何もしない")
        write_tmp("fetch_earnings_out", {"events": [], "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        return 0

    resolved, missing = _resolve_dates(holds)

    updated = 0
    for h in resolved:
        if update_portfolio_date(client, h, h["edate"], args.dry_run):
            updated += 1

    events = [build_event(h, h["edate"]) for h in resolved]
    write_tmp("fetch_earnings_out", {
        "events": events,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    # 売却された銘柄の決算イベントを掃除（投資家カレンダーDB）
    cur_ids = {e["id"] for e in events}
    archived = archive_stale(client, get_notion_db_id(), cur_ids, args.dry_run)

    mode = "dry-run" if args.dry_run else "実行"
    log(f"fetch_earnings 完了({mode}): events={len(events)}, "
        f"ポートフォリオ更新={updated}, 取得失敗={len(missing)}, 掃除={archived}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
