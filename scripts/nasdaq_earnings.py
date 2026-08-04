# -*- coding: utf-8 -*-
"""
Nasdaq 公式決算カレンダー（無料・APIキー不要）から
「銘柄 → 次回決算日 ＋ 寄り前/引け後」を取得する補助モジュール。

  https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD

用途（fetch_earnings.py から呼ぶ）:
  1. 米国株の発表セッション（寄り前 BMO / 引け後 AMC）の確定。
     yfinance の get_earnings_dates から推測していたが本番で機能しておらず、
     カレンダー上の米国株が全部 16:00 ET（＝翌朝5:00 JST）になっていた。
  2. 決算「日」のクロスチェック。yfinance には
     「引け後銘柄で1日ズレる」「決算通過直後に前回の日付を返す」既知の癖がある。

⚠️ 掲載範囲は約5週間先まで（実測: 5週先=243件, 6週先=8件, 10週先=0件）。
   それより先の決算はこのAPIでは取れないので、従来どおり yfinance／予測に頼る。
   ＝「近い決算ほど正確になる」設計。

実行（単体確認）:
  python scripts/nasdaq_earnings.py --self-test
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import log, record_fetch_warning  # noqa: E402

API = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
# ブラウザ相当の UA を付けないと弾かれる（bot 保護）
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
# 何日先まで見るか（Nasdaq の掲載範囲＝約5週間より少し広めに取る）
DEFAULT_DAYS_AHEAD = 45
# 連続でこの回数失敗したら以降は諦める（Actions の IP が弾かれた場合など）
MAX_CONSECUTIVE_ERRORS = 5

# Nasdaq の time 表記 → 本パイプラインのセッション表記
SESSION_MAP = {
    "time-pre-market": "AM",    # 寄り前
    "time-after-hours": "PM",   # 引け後
}


def _fetch_day(d: date) -> list[dict]:
    """指定日の決算予定行リストを返す（失敗時は例外）。"""
    req = urllib.request.Request(API.format(d=d.isoformat()), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data") or {}
    return data.get("rows") or []


def nasdaq_earnings_map(today: date | None = None,
                        days_ahead: int = DEFAULT_DAYS_AHEAD) -> dict[str, dict]:
    """{ティッカー: {"date": date, "session": "AM"/"PM"/None}} を返す。

    同じ銘柄が複数日に出ることは基本ないが、出た場合は最も早い日を採用する。
    ベストエフォート（全部失敗しても空 dict を返し、呼び出し側は従来動作に落ちる）。
    """
    today = today or date.today()
    out: dict[str, dict] = {}
    ok_days = 0
    errors = 0
    consecutive = 0
    for i in range(days_ahead + 1):
        d = today + timedelta(days=i)
        if d.weekday() >= 5:      # 土日は決算発表なし
            continue
        try:
            rows = _fetch_day(d)
            consecutive = 0
            ok_days += 1
        except Exception as e:
            errors += 1
            consecutive += 1
            log(f"  Nasdaq {d} 取得失敗: {type(e).__name__}: {e}")
            if consecutive >= MAX_CONSECUTIVE_ERRORS:
                log("  Nasdaq 連続失敗 → 以降スキップ（セッション判定は従来ロジックに落ちる）")
                record_fetch_warning("nasdaq_earnings",
                                     f"{consecutive}日連続で取得失敗（{d} 時点で打ち切り）")
                break
            continue
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper()
            if not sym:
                continue
            prev = out.get(sym)
            if prev and prev["date"] <= d:
                continue
            out[sym] = {"date": d, "session": SESSION_MAP.get(r.get("time") or "")}
    log(f"  Nasdaq 決算カレンダー: {len(out)} 銘柄 / 取得成功 {ok_days} 日・失敗 {errors} 日")
    if ok_days == 0:
        record_fetch_warning("nasdaq_earnings", "全日取得失敗（セッション判定なしで継続）")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Nasdaq 決算カレンダーの取得確認")
    ap.add_argument("--self-test", action="store_true", help="数銘柄を表示して確認")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS_AHEAD)
    args = ap.parse_args()
    m = nasdaq_earnings_map(days_ahead=args.days)
    for sym in ("NVDA", "AAPL", "MCD", "KMB", "HD", "DG", "ZTS", "KDP", "DIS"):
        if sym in m:
            log(f"    {sym}: {m[sym]['date']} session={m[sym]['session']}")
    return 0 if m else 1


if __name__ == "__main__":
    raise SystemExit(main())
