# -*- coding: utf-8 -*-
"""
保有株の次回決算日を取得して:
  (A) ポートフォリオ管理DB(Notion)の「次回決算日」プロパティを更新
  (B) 投資家カレンダー用の「決算」イベントを tmp/fetch_earnings_out.json に出力
する。

データ源（日付）:
  - JPX 決算発表予定Excel（日本株の第一候補）: 取引所公式の発表予定＝確定扱い(is_estimated=false)。
    ただし直近約1ヶ月分しか掲載されない
  - Nasdaq 公式決算カレンダー（米国株の第一候補・scripts/nasdaq_earnings.py）:
    約5週間先まで掲載。寄り前/引け後の別も分かる
  - yfinance（上記の圏外フォールバック）: Ticker.calendar["Earnings Date"]（日付のみ採用）
  - J-Quants 予測日 data/jq_earnings_jp.json（日本株の最終フォールバック）:
    jquants-bulk/build_earnings_estimates.py が10年開示履歴から生成した
    「昨年同四半期の実開示日+364日」の予測。JPX予定Excelは直近約1ヶ月分しか
    無いため、決算通過後〜次回掲載までの空白期間もイベントが消えないようにする

データ源（時刻）:
  未来の発表時刻を事前公表する公式ソースは存在しない（会社が出すのは日付だけ）ため、
  「その銘柄が過去いつも何時に出しているか」から埋める。何ヶ月先の予定にも使える。
  - 日本株: J-Quants の開示時刻実績 data/jq_earnings_jp.json の disc_times
    （実績では 15:30 が45%・16:00 が15%で、従来の決め打ち 15:00 は7%しかなかった）
  - 米国株: SEC EDGAR の 8-K(Item 2.02) 受理時刻の実績（scripts/us_earnings_time.py）
    取れない銘柄は寄り前=07:00 ET / 引け後・不明=16:00 ET の既定値

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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nasdaq_earnings import nasdaq_earnings_map  # noqa: E402
from us_earnings_time import us_earnings_time_map  # noqa: E402
from common import (  # noqa: E402
    NotionClient,
    record_fetch_warning,
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

# 採用した決算日と「実績からの予測」がこの日数以上ズレたら要注意として印を付ける
CROSS_CHECK_TOLERANCE_DAYS = 4

# 1四半期の日数の目安（次の決算がどのあたりに来るかの当たりを付けるのに使う）
QUARTER_DAYS = 91

# 起点（前回発表日）から次の四半期を数えるときの猶予日数。
# 決算当日〜直後は「前回発表 + 91日」がまだ今日より手前なので、そのまま数えると
# 1つ先へ飛んでしまう。今日の15日前までは「まだその四半期」とみなす。
ANCHOR_GRACE_DAYS = 15

# J-Quants 予測ファイルがこの日数より古くなったら警告する（約1年ぶんの予測しかない）
JQ_ESTIMATES_MAX_AGE_DAYS = 300

# ⚠️ このリポジトリは公開。GitHub Actions の実行ログも誰でも読める。
# 銘柄名・ティッカー入りの行をそのまま出すと保有銘柄が丸見えになるため
# （2026-08-06 の investor_events.json 公開事故と同じ穴）、
# Actions 上では銘柄単位の行を出さず、件数の集計だけをログに残す。
# ローカル実行（手元でのデバッグ）では従来どおり全部出す。
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


def detail_log(msg: str) -> None:
    """銘柄名が入るログ。公開ログ（Actions）では出さない。"""
    if not IN_ACTIONS:
        log(msg)

# 「保有中」とみなすステータス（これ以外＝未購入/売却予定/売却済 は対象外）
HELD_STATUSES = {"保有継続", "目標達成", "部分達成", "打診買い済"}

TZ_JST = ZoneInfo("Asia/Tokyo")
TZ_ET = ZoneInfo("America/New_York")

# 決算発表時刻のデフォルト（銘柄ごとの実績が取れなかったときだけ使う）。
#   日本株 … J-Quants の開示実績（jq_earnings_jp.json の disc_times）を優先。
#            実績では 15:30 が45%・16:00 が15%で、15:00 ちょうどは7%しかない。
#   米国株 … SEC EDGAR の 8-K(Item 2.02) 受理時刻の実績を優先。
#            取れなければ寄り前(BMO)=07:00 ET / 引け後(AMC)・不明=16:00 ET。
#            セッションは Nasdaq 公式カレンダー（約5週間先まで）→ yfinance の順で判定。
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
    # --- 2026-07-29 追加: S&P500+MidCap400の903銘柄スクリーニング通過24銘柄のうち、
    #     打診ラダーと「決算の合格条件」を事前登録した3銘柄。
    #     決算がそのままエントリー判定のゲートになるため、日付を必ずカレンダーへ出す。
    {"name": "ブロードリッジ", "ticker": "BR", "market": "米国",
     "page_id": None, "current": None, "date_prop": "次回決算日", "is_watch": True},
    {"name": "インターコンチネンタル取引所", "ticker": "ICE", "market": "米国",
     "page_id": None, "current": None, "date_prop": "次回決算日", "is_watch": True},
    {"name": "インテュイット", "ticker": "INTU", "market": "米国",
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


def _yf_next_row(tk) -> tuple[date | None, str | None]:
    """get_earnings_dates から「今日(ET)以降で最も近い行」の (日付, セッション) を返す。

    ⚠️ calendar["Earnings Date"] を使ってはいけない。
    Nasdaq 公式カレンダーを正解として150銘柄で突き合わせた実測（2026-08-26）:
        calendar        寄り前(AM)銘柄 ピタリ 90% / 引け後(PM)銘柄 ピタリ  0%（27件全部+1日）
        この関数        寄り前(AM)銘柄 ピタリ100% / 引け後(PM)銘柄 ピタリ100%
    calendar は引け後発表を「翌日」として返すため、AMC銘柄の決算日が
    まるごと1日後ろにズレていた。行の時刻から日付とセッションを直接読む。
    """
    try:
        df = tk.get_earnings_dates(limit=16)
        if df is None or len(df) == 0:
            return None, None
        today_et = datetime.now(TZ_ET).date()
        future = sorted(ix for ix in df.index if ix.date() >= today_et)
        if not future:
            return None, None
        ix = future[0]
        # 00:00 は「時刻不明」のプレースホルダなのでセッションは分からない扱い
        session = None if (ix.hour == 0 and ix.minute == 0) else ("AM" if ix.hour < 12 else "PM")
        return ix.date(), session
    except Exception:
        return None, None


def yf_next_earnings(symbol: str, want_session: bool = False) -> tuple[date | None, str | None]:
    """yfinance で次回決算予定日(date)と発表セッション('AM'/'PM'/None)を返す。

    第一候補は get_earnings_dates の直近未来行（上記のとおり日付が正確）。
    行が無いときだけ calendar["Earnings Date"] に落ちる（引け後銘柄は1日後ろに
    ズレうるので、その場合はセッションから補正する）。
    取れなければ (None, None)。"""
    if not symbol:
        return None, None
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        edate, session = _yf_next_row(tk)
        if edate is not None:
            return edate, (session if want_session else None)
        cal = tk.calendar
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not ed:
            return None, None
        if isinstance(ed, (list, tuple)):
            ed = ed[0] if ed else None
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
        if session == "PM":
            # calendar は引け後発表を翌日として返すので1日戻す
            edate -= timedelta(days=1)
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
    {4桁コード: 次回の予測日} を返す。ベストエフォート（無ければ空 dict）。

    ファイルには銘柄ごとに4四半期ぶんの候補日（昨年同四半期の実開示日+364日）と、
    起点になる last_disc（最後の実開示日）が入っている。候補を1つに絞る計算は
    米国株の EDGAR 予測とまったく同じ pick_next_date（anchored）で行う。
    last_disc が無い古いファイルのときだけ「今日以降で最も早い候補」に落ちる。
    予測なので±数日ズレうる（is_estimated=true 前提）。
    """
    out: dict[str, date] = {}
    today = today or date.today()
    try:
        payload = json.loads(JQ_ESTIMATES_PATH.read_text(encoding="utf-8"))
        last_disc = payload.get("last_disc") or {}
        n_no_anchor = 0
        for code, dates in payload.get("estimates", {}).items():
            cands = []
            for d in dates:
                try:
                    cands.append(date.fromisoformat(str(d)[:10]))
                except (ValueError, TypeError):
                    continue
            anchor = None
            try:
                anchor = date.fromisoformat(str(last_disc[code])[:10])
            except (KeyError, ValueError, TypeError):
                n_no_anchor += 1
            picked = pick_next_date(cands, today, anchor=anchor)
            if picked is not None:
                out[code] = picked
        gen = str(payload.get("generated_at") or "")
        log(f"  J-Quants 予測マップ {len(out)} 件 (generated_at={gen})")
        if n_no_anchor:
            # 起点が無い＝生成側が古い。素朴版に落ちているので数を出しておく
            # （全滅なら build_earnings_estimates.py を作り直すサイン）。
            # ::warning:: は行頭でないと Actions が注釈として拾わないので字下げしない
            msg = (f"J-Quants 予測の起点(last_disc)が無い銘柄 {n_no_anchor} 件 → "
                   f"その銘柄だけ「最も早い候補」で代用。"
                   f"jquants-bulk/build_earnings_estimates.py を実行して更新すること")
            log(f"::warning::{msg}")
            record_fetch_warning("fetch_earnings", msg)
        # このファイルは手動再生成（jquants-bulk/build_earnings_estimates.py）で
        # 約1年ぶんの予測しか入っていない。切れると日本株の最終フォールバックが
        # 黙って効かなくなるので、古くなったら警告を出す（→ health-watchdog が LINE 通知）。
        try:
            age = (today - date.fromisoformat(gen[:10])).days
            if age > JQ_ESTIMATES_MAX_AGE_DAYS:
                msg = (f"J-Quants 予測ファイルが古い（{gen[:10]} 生成・{age}日経過）。"
                       f"jquants-bulk/build_earnings_estimates.py を実行して更新すること")
                log(f"::warning::{msg}")
                record_fetch_warning("fetch_earnings", msg)
        except ValueError:
            pass
    except FileNotFoundError:
        log("  J-Quants 予測ファイルなし → フォールバックなしで継続")
    except Exception as e:
        log(f"  J-Quants 予測読込失敗: {type(e).__name__}: {e} → フォールバックなしで継続")
    return out


def pick_next_date(cands, today: date, anchor: date | None = None,
                   rule: str = "anchored") -> date | None:
    """候補日の中から「次回の決算発表日」を1つ選ぶ（日米・全ソース共通のロジック）。

    cands  … 候補日（各過去実績 +364日）。今日より前のものは捨てる
    anchor … 前回の実発表日。anchored ルールで「次の四半期の位置」を見積もる起点
    rule="anchored"（既定）:
      「anchor + 91日×n」（今日の15日前以降になる最小の n）に最も近い候補を選ぶ。
      素朴に「今日以降で最も早い候補」を採ると、履歴に決算以外の開示が1件混ざる
      だけで3ヶ月手前の日付を拾ってしまう。
    rule="nearest" または anchor が無いとき:
      今日以降で最も早い候補（比較検証用の素朴版・起点が取れないときの保険）。

    アウトオブサンプル検証の差（2026-08-27 実測・詳細は README）:
      米国株454件  ピタリ 44.1%→49.3% / ±7日 82.8%→91.2% / 平均ズレ 12.1日→4.9日
      日本株137,593件 ピタリ 30.8%→33.4% / ±7日 89.2%→96.2% / 平均ズレ 8.9日→2.5日
    """
    future = sorted(c for c in set(cands) if c >= today)
    if not future:
        return None
    if rule == "nearest" or anchor is None:
        return future[0]
    # 前回発表からいくつ四半期を進めれば「次の決算」になるかを見積もる。
    # 15日の猶予は「決算日当日〜直後」にまだ前の四半期を指してしまうのを防ぐため。
    n = 1
    while anchor + timedelta(days=QUARTER_DAYS * n) < today - timedelta(days=ANCHOR_GRACE_DAYS):
        n += 1
    target = anchor + timedelta(days=QUARTER_DAYS * n)
    return min(future, key=lambda c: abs((c - target).days))


def next_from_history(past_dates: list[str], today: date,
                      cycle_days: int = 364, rule: str = "anchored") -> date | None:
    """過去の決算発表日（"YYYY-MM-DD" のリスト）から次回発表日を予測する。

    ルールは「昨年の同じ四半期の実発表日 + 364日」。
    364 = 52週ちょうどなので曜日が保たれる（決算発表は曜日のクセが強い）。
    候補の絞り込みは pick_next_date（日米共通）に任せる。

    過去4本ぶん（＝1年分）に満たないときは予測しない（当てずっぽうになるため）。
    """
    days: list[date] = []
    for d in past_dates:
        try:
            days.append(date.fromisoformat(str(d)[:10]))
        except (ValueError, TypeError):
            continue
    if len(days) < 4:
        return None
    # 候補は「各過去実績 +364日」。起点は最後に発表した日
    return pick_next_date((d + timedelta(days=cycle_days) for d in days),
                          today, anchor=max(days), rule=rule)


JP_HOLIDAYS_PATH = Path(__file__).resolve().parents[1] / "data" / "jp_market_holidays.json"
_jp_closed: set[date] | None = None
_jp_closed_until: date | None = None


def jp_closed_days() -> set[date]:
    """東証が休場する平日（祝日）の集合。data/jp_market_holidays.json から読む。

    元データは J-Quants の取引カレンダー（＝JPX の営業日区分）。約1年先まで。
    切れたら警告を出す（→ health-watchdog が LINE 通知）。
    """
    global _jp_closed, _jp_closed_until
    if _jp_closed is not None:
        return _jp_closed
    _jp_closed = set()
    try:
        payload = json.loads(JP_HOLIDAYS_PATH.read_text(encoding="utf-8"))
        _jp_closed = {date.fromisoformat(d) for d in payload.get("closed", [])}
        until = payload.get("coverage_until")
        _jp_closed_until = date.fromisoformat(until) if until else None
        log(f"  東証休場日 {len(_jp_closed)} 件（{until} まで）")
        if _jp_closed_until and (_jp_closed_until - date.today()).days < 120:
            msg = (f"東証休場日リストの期限が近い（{until} まで）。"
                   f"jquants-bulk の trading_calendar から作り直すこと")
            log(f"::warning::{msg}")
            record_fetch_warning("fetch_earnings", msg)
    except FileNotFoundError:
        log("  東証休場日リストなし → 祝日の補正はしない")
    except Exception as e:
        log(f"  東証休場日リスト読込失敗: {type(e).__name__}: {e}")
    return _jp_closed


def snap_to_open_day(ed: date, market: str, name: str) -> date:
    """決算日が市場の休場日なら翌営業日へずらす。

    予測（昨年の実発表日+364日）は曜日を保つが祝日は考えないので、
    その年だけ祝日に当たることがある（例: 任天堂は昨年11/4(火)発表→
    +364日で 2026-11-03 になるが、これは文化の日で東証は休場）。
    土日も同じ理屈でずらす。ずらす先は「次に開く日」。
    """
    closed = jp_closed_days() if market == "日本" else set()
    us_holidays: set[date] = set()
    if market != "日本":
        try:
            from fetch_schedules import us_federal_holidays  # 遅延import（重いので必要時だけ）
            us_holidays = us_federal_holidays(ed.year) | us_federal_holidays(ed.year + 1)
        except Exception:
            us_holidays = set()
    closed = closed | us_holidays
    moved = ed
    for _ in range(10):
        if moved.weekday() < 5 and moved not in closed:
            break
        moved += timedelta(days=1)
    if moved != ed:
        detail_log(f"    {name}: {ed} は休場日のため {moved} に補正")
    return moved


def _future_only(ed: date | None, today: date, label: str, name: str) -> date | None:
    """過去日を弾く共通ガード。

    yfinance は決算通過直後に「前回の決算日」を返し続けることがある
    （既知の癖。2026-07 の YUM 7/30 など）。これを素通しすると
    終わった決算がカレンダーに残り続けるので、必ずここを通す。
    """
    if ed is None:
        return None
    if ed < today:
        detail_log(f"    {name}: {label} が過去日 {ed} を返したため不採用（次の候補へ）")
        return None
    if ed.weekday() >= 5:
        # 土日に決算発表はしない（取引所が閉まっている）。明らかな誤りなので捨てる
        detail_log(f"    {name}: {label} が土日 {ed} を返したため不採用（次の候補へ）")
        return None
    return ed


def jq_disc_time_map() -> dict[str, str]:
    """
    data/jq_earnings_jp.json の disc_times（銘柄ごとの開示時刻の実績）を読み、
    {4桁コード: "HH:MM"} を返す。無ければ空 dict（＝従来の 15:00 デフォルトに落ちる）。

    決算発表の「時刻」を事前公表する公式ソースは存在しないため、
    その銘柄が過去いつも何時に開示しているか（J-Quants の DiscTime 実績）で埋める。
    """
    out: dict[str, str] = {}
    try:
        payload = json.loads(JQ_ESTIMATES_PATH.read_text(encoding="utf-8"))
        for code, v in (payload.get("disc_times") or {}).items():
            t = (v or {}).get("time")
            if isinstance(t, str) and re.fullmatch(r"\d{2}:\d{2}", t):
                out[code] = t
        log(f"  J-Quants 開示時刻マップ {len(out)} 件")
    except FileNotFoundError:
        log("  J-Quants 予測ファイルなし → 日本株の時刻は既定値で継続")
    except Exception as e:
        log(f"  J-Quants 開示時刻読込失敗: {type(e).__name__}: {e} → 既定値で継続")
    return out


# ----------------------------------------------------------------------
# イベント生成
# ----------------------------------------------------------------------
def _split_hhmm(hhmm: str | None, def_h: int, def_m: int) -> tuple[int, int]:
    """"HH:MM" を (時, 分) に分解する。取れていなければ既定値を返す。"""
    if isinstance(hhmm, str) and re.fullmatch(r"\d{2}:\d{2}", hhmm):
        return int(hhmm[:2]), int(hhmm[3:])
    return def_h, def_m


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
    # 銘柄ごとの実績から求めた発表時刻（"HH:MM"）。無ければ市場ごとの既定値。
    hhmm = h.get("time_hhmm")
    if market == "日本":
        country = "JP"
        code = to_jp_code(ticker)
        # id は銘柄ごとに固定（日付を含めない＝決算日が動いても upsert で更新・重複しない）
        ev_id = f"{prefix}_jp_{code}"
        hh, mm = _split_hhmm(hhmm, JP_HOUR, JP_MIN)
        local = f"{edate.isoformat()}T{hh:02d}:{mm:02d}:00+09:00"
        tz = "Asia/Tokyo"
        src = f"https://finance.yahoo.co.jp/quote/{code}.T"
    else:
        country = "US"
        sym = ticker.strip().upper()
        ev_id = f"{prefix}_us_{sym}"
        # 実績時刻があればそれを使い、無ければ
        # 寄り前(AM/BMO)=07:00 ET、引け後(PM/AMC)・不明=16:00 ET で出し分け
        if h.get("session") == "AM":
            hh, mm = _split_hhmm(hhmm, US_BMO_HOUR, US_BMO_MIN)
        else:
            hh, mm = _split_hhmm(hhmm, US_HOUR, US_MIN)
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
        # 日付がどこから来たか（JPX/Nasdaq=取引所公式、yfinance=Yahoo推定、
        # JQ予測/EDGAR予測=その銘柄の過去実績からの推定）。
        # 後から「どのソースが何日ズレたか」を数えられるようにするための記録で、
        # 説明欄にも出す（Notion DB は非公開なのでここは伏せない）。
        "date_source": h.get("src") or "不明",
        "description": _describe(kind, name, h),
        "source_url": src,
        "result": None,
    }


# 日付ソースの説明（Notion の説明欄・アプリの詳細画面に出る）
SOURCE_LABELS = {
    "JPX": "JPX（東証）公式の発表予定日",
    "Nasdaq": "Nasdaq公式決算カレンダーの予定日",
    "yfinance": "Yahoo Finance の予定日（推定を含む）",
    "JQ予測": "過去の開示実績からの予測日（昨年同四半期+364日）",
    "EDGAR予測": "SEC 8-K の発表実績からの予測日（昨年同四半期+364日）",
}


def _describe(kind: str, name: str, h: dict) -> str:
    """イベントの説明文。出どころと、他ソースとの食い違いを明記する。"""
    src = h.get("src") or "不明"
    label = SOURCE_LABELS.get(src, f"出典 {src}")
    tail = "" if src == "JPX" else "。予定日は変更される場合があります"
    gap = h.get("cross_gap")
    warn = ""
    if gap:
        warn = (f"。⚠️ 過去実績からの予測とは{abs(gap)}日ズレています"
                f"（{'後ろ' if gap > 0 else '前'}倒し方向）")
    return f"{kind}の決算発表予定（{name}）。{label}{tail}{warn}。"


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
            detail_log(f"  skip(重複・無効): {name}")
            continue
        ticker = read_rich_text(pr.get(p_ticker, {})).strip()
        market = read_select(pr.get(p_market, {}))
        if not ticker or market == "ETF" or not market:
            continue
        # 投信・債券など yfinance で扱えない日本語コードを除外
        if not is_valid_ticker(ticker, market):
            detail_log(f"  skip(非対応ティッカー): {name}({ticker})")
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
    """各保有株の決算「日」と「時刻」を解決する。

    日付（上から順に試し、最初に取れたものを採用）:
      日本株: JPX公式（確定・約1ヶ月先まで）→ yfinance → J-Quants予測
      米国株: Nasdaq公式カレンダー（約5週間先まで）→ yfinance → EDGAR実績予測
    どのソースでも「今日以降」でなければ採用しない（_future_only）。
    yfinance は決算通過直後に前回の決算日を返す癖があるため、このガードが無いと
    終わった決算がカレンダーに残り続ける。
    時刻（未来の時刻を事前公表する公式ソースは無いので実績から推定）:
      日本株: J-Quants の開示時刻実績（銘柄ごと）→ 既定 15:00 JST
      米国株: SEC EDGAR の 8-K(2.02) 受理時刻実績 → セッション既定(07:00/16:00 ET)
    JPX は取引所の公式発表予定なので is_estimated=false（確定）として扱える。
    (date付きリスト, 取得失敗リスト) を返す。"""
    jpx: dict[str, date] = {}
    jq: dict[str, date] = {}
    jq_times: dict[str, str] = {}
    if any(h["market"] == "日本" for h in holds):
        jpx = jpx_earnings_map()
        jq = jq_estimates_map()
        jq_times = jq_disc_time_map()
    us_syms = sorted({(h["ticker"] or "").strip().upper()
                      for h in holds if h["market"] != "日本"})
    nas: dict[str, dict] = {}
    edgar: dict[str, dict] = {}
    if us_syms:
        nas = nasdaq_earnings_map()
        edgar = us_earnings_time_map(us_syms)
    # JPXのExcelには発表直後の過去日が残っていることがあるため「今日以降」だけ採用する
    today_jst = datetime.now(TZ_JST).date()
    today_et = datetime.now(TZ_ET).date()
    resolved: list[dict] = []
    missing: list[dict] = []
    for h in holds:
        sym = to_yf_symbol(h["ticker"], h["market"])
        is_jp = h["market"] == "日本"
        name = h["name"]
        ed: date | None = None
        session: str | None = None
        time_hhmm: str | None = None
        alt: date | None = None      # 別ソースの独立予測（クロスチェック用）
        src = ""
        time_src = "既定"
        if is_jp:
            code = to_jp_code(h["ticker"])
            alt = jq.get(code)  # J-Quants 開示履歴からの予測（今日以降で絞り込み済み）
            # 日本株はまずJPX公式（確定日）を見る
            jpx_d = jpx.get(code)
            if jpx_d is not None and jpx_d >= today_jst:
                ed, src = jpx_d, "JPX"
            if ed is None:
                yd, _ = yf_next_earnings(sym, want_session=False)
                ed = _future_only(yd, today_jst, "yfinance", name)
                if ed is not None:
                    src = "yfinance"
            if ed is None and alt is not None:
                # 最終フォールバック: J-Quants 開示履歴からの予測日（±数日ズレうる）
                ed, src = alt, "JQ予測"
            # 時刻は日付の出どころに関係なく、その銘柄の開示実績を使う
            if jq_times.get(code):
                time_hhmm, time_src = jq_times[code], "JQ実績"
        else:
            usym = (h["ticker"] or "").strip().upper()
            erow = edgar.get(usym)
            # EDGAR の 8-K(2.02) 実績から作る独立予測（昨年同四半期+364日）
            alt = next_from_history((erow or {}).get("dates") or [], today_et)
            nrow = nas.get(usym)
            if nrow and nrow["date"] >= today_et:
                # Nasdaq 公式カレンダー（掲載範囲内＝約5週間先まで）を優先
                ed, session, src = nrow["date"], nrow["session"], "Nasdaq"
            if ed is None:
                yd, ysess = yf_next_earnings(sym, want_session=True)
                yd = _future_only(yd, today_et, "yfinance", name)
                if yd is not None:
                    ed, session, src = yd, ysess, "yfinance"
            if ed is None and alt is not None:
                # 公式カレンダーの圏外（約5週より先）で yfinance も駄目なときの受け皿。
                # 銘柄自身の過去の発表日から作るので何ヶ月先でも埋まる。
                ed, src = alt, "EDGAR予測"
            if erow:
                # セッションが分かっていて EDGAR 実績と食い違う銘柄は信用しない
                # （プレスから遅れて 8-K を出す会社を誤って拾わないため）
                if session and erow.get("session") and erow["session"] != session:
                    detail_log(f"    {name}: EDGAR実績({erow['session']})と"
                               f"カレンダー({session})が不一致 → 時刻は既定値を使用")
                else:
                    session = session or erow.get("session")
                    if erow.get("time"):
                        time_hhmm, time_src = erow["time"], "EDGAR実績"
        if ed is None:
            missing.append(h)
            detail_log(f"  {name}({h['ticker']}): 決算日が取得できず（スキップ）")
            continue
        # 公式以外（推定・予測）の日付は、休場日に当たっていたら翌営業日へずらす。
        # 公式（JPX/Nasdaq）は取引所が出した日程そのものなので触らない。
        if src not in ("JPX", "Nasdaq"):
            ed = snap_to_open_day(ed, h["market"], name)
        # クロスチェック: 採用した日付と、独立に作った実績予測を突き合わせる。
        # ズレていても落とさない（どちらが正しいかは決められない）。
        #   ・公式（JPX/Nasdaq）とのズレ … 予測ルールの出来を測る材料。ログだけ
        #   ・yfinance とのズレ           … どちらも推定なので説明欄に ⚠️ を出す
        gap = None
        if alt is not None and src not in ("JQ予測", "EDGAR予測"):
            diff = (ed - alt).days
            # 差が四半期のスケール（45日超）なら、そもそも別の四半期を指している。
            # 決算当日は実績予測が「次の回」を向くのでズレて当たり前＝警告しない。
            if CROSS_CHECK_TOLERANCE_DAYS <= abs(diff) <= 45:
                detail_log(f"    {name}: {src}={ed} と実績予測={alt} が "
                           f"{diff:+d}日ズレ")
                if src == "yfinance":
                    gap = diff
        h2 = dict(h)
        h2["edate"] = ed
        h2["src"] = src
        h2["cross_gap"] = gap        # None か、実績予測との日数差
        h2["session"] = session      # 'AM'/'PM'/None（時刻の既定値の出し分けに使う）
        h2["time_hhmm"] = time_hhmm  # "HH:MM"/None（build_event が使う）
        resolved.append(h2)
        sess_label = f" [{session}]" if session else ""
        detail_log(f"  {name}({h['ticker']}) -> {ed} [{src}]{sess_label} "
                   f"時刻={time_hhmm or '既定'}({time_src})")
    _log_source_mix(resolved)
    return resolved, missing


def _log_source_mix(resolved: list[dict]) -> None:
    """決算日がどのソースから来たかの内訳をログに出す（精度の健康診断）。

    公式（JPX/Nasdaq）の比率が高いほど信用でき、yfinance/予測が多い時期は
    「まだ確定していない先の決算を見ている」ということ。銘柄名は出さないので
    公開リポジトリの Actions ログに出しても差し支えない。
    """
    if not resolved:
        return
    mix: dict[str, int] = {}
    for h in resolved:
        mix[h.get("src") or "不明"] = mix.get(h.get("src") or "不明", 0) + 1
    official = sum(v for k, v in mix.items() if k in ("JPX", "Nasdaq"))
    detail = " / ".join(f"{k} {v}" for k, v in sorted(mix.items(), key=lambda kv: -kv[1]))
    log(f"  日付ソース内訳: {detail} （公式 {official}/{len(resolved)}件 "
        f"= {official * 100 // len(resolved)}%）")
    warn = sum(1 for h in resolved if h.get("cross_gap"))
    if warn:
        log(f"  ※ yfinance の日付が実績予測と{CROSS_CHECK_TOLERANCE_DAYS}日以上"
            f"ズレている銘柄 {warn} 件（説明欄に ⚠️ を出す）")


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
        # ウォッチ銘柄のどれを実際に買ったかは保有情報なので公開ログには出さない
        detail_log(f"  ウォッチ銘柄のうち保有済みのため除外: {', '.join(dropped)}")
        log(f"  ウォッチ銘柄のうち保有済みのため除外: {len(dropped)} 件")
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
