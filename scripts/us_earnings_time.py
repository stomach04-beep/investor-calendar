# -*- coding: utf-8 -*-
"""
米国株の「決算をいつも何時に出すか」を SEC EDGAR の実績から推定する補助モジュール。

決算プレスリリースは Form 8-K の Item 2.02（Results of Operations）として提出される。
EDGAR の submissions API には各提出の受理時刻が分単位で残っているので、
銘柄ごとの直近の 8-K(2.02) 受理時刻の最頻値＝その銘柄の発表時刻とみなす。

  https://data.sec.gov/submissions/CIK##########.json   （無料・APIキー不要）

なぜ「過去の実績」から作るのか:
  未来の決算発表時刻を事前公表する公式ソースは存在しない（会社が出すのは日付だけ）。
  Nasdaq でも分かるのは寄り前/引け後の別だけ、しかも約5週間先まで。
  一方この方法は銘柄のクセなので、何ヶ月先の予定にも使える。

⚠️ acceptanceDateTime のタイムゾーン表記に不整合がある（末尾 Z だが実質 ET の行が混ざる）。
   実測すると、UTC と解釈すべき銘柄（AAPL 16:30・NVDA 16:21・KO 06:58・PG 07:03）と、
   そのまま ET と解釈すべき銘柄（JNJ 07:4x・KMB 06:33）が混在する。
   そこで「決算発表としてあり得る時刻か」（EDGAR受理時間帯 6:00-22:00 ET かつ
   場中 9:30-16:00 ET でない）で両解釈をふるいにかけ、残った方を採用する。
   このルールは実測20銘柄160件で既知の発表時刻と矛盾しないことを確認済み。

実行（単体確認）:
  python scripts/us_earnings_time.py --self-test
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import log, record_fetch_warning  # noqa: E402

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
# SEC は「連絡先メールアドレス入りの User-Agent」を要求しており、
# 形式が合わないと 403 になる（URL を含む UA も弾かれる。github.com も不可）。
# 連絡先は環境変数 SEC_CONTACT_EMAIL で差し替え可能（GitHub Secret 推奨。
# 公開リポジトリに個人アドレスを直書きしないため既定はプレースホルダ）。
_CONTACT = os.environ.get("SEC_CONTACT_EMAIL", "").strip() or "admin@example.com"
UA = f"investor-calendar-bot/1.0 {_CONTACT}"

TZ_ET = ZoneInfo("America/New_York")
TZ_UTC = ZoneInfo("UTC")

# 銘柄ごとに何件の 8-K(2.02) をさかのぼるか（＝直近2年ぶん程度）
SAMPLES = 8
MIN_SAMPLES = 3
# SEC のレート制限（10req/秒）に余裕を持たせる待ち時間
SLEEP_SEC = 0.15


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ticker_to_cik() -> dict[str, str]:
    """{ティッカー: 10桁CIK} を SEC 公式一覧から作る。失敗時は空 dict。"""
    try:
        raw = _get_json(TICKER_MAP_URL)
    except Exception as e:
        log(f"  SEC ティッカー一覧の取得失敗: {type(e).__name__}: {e}")
        record_fetch_warning("us_earnings_time", "SECティッカー一覧の取得に失敗")
        return {}
    out: dict[str, str] = {}
    for row in raw.values():
        t = str(row.get("ticker") or "").strip().upper()
        c = str(row.get("cik_str") or "").strip()
        if t and c:
            out.setdefault(t, c.zfill(10))
    log(f"  SEC ティッカー→CIK: {len(out)} 件")
    return out


def _minutes(t: datetime) -> int:
    return t.hour * 60 + t.minute


def _is_release_time(t: datetime) -> bool:
    """決算プレスの公表時刻としてあり得る ET 時刻か。

    寄り前 6:00-9:30 か、引け後 16:00-17:30 のみを認める。
    ここを「場中でなければ何時でも可」と広く取ると、プレスから数時間遅れて
    8-K を出す銘柄（PEP は 6:00 発表・14:00 提出）で誤った時刻を拾ってしまう。
    """
    m = _minutes(t)
    return (6 * 60 <= m <= 9 * 60 + 30) or (16 * 60 <= m <= 17 * 60 + 30)


def _is_intraday(t: datetime) -> bool:
    """場中（寄り〜引け）か。＝プレスは朝、8-K提出だけ遅れたと推測できる。"""
    return 9 * 60 + 30 < _minutes(t) < 16 * 60


def interpret_acceptance(acc: str) -> tuple[datetime | None, bool]:
    """acceptanceDateTime を (公表時刻とみなせるET時刻, 場中提出フラグ) に解釈する。

    末尾 Z だが UTC 解釈が正しい行と ET 解釈が正しい行が混在するため、
    両方を作って「公表時刻としてあり得る方」を採る。
    両方あり得る場合は UTC 解釈を優先（AAPL 16:30・NVDA 16:21 で実測一致）。
    どちらも公表時刻らしくないが UTC 解釈が場中なら、
    「朝に発表して昼に提出した」とみなし寄り前(AM)の票としてだけ使う。
    """
    try:
        naive = datetime.strptime(acc[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None, False
    as_utc = naive.replace(tzinfo=TZ_UTC).astimezone(TZ_ET).replace(tzinfo=None)
    as_et = naive
    if _is_release_time(as_utc):
        return as_utc, False
    if _is_release_time(as_et):
        return as_et, False
    if _is_intraday(as_utc):
        return None, True
    return None, False


def _recent_8k_202(cik: str) -> list[dict]:
    """8-K の Item 2.02（＝決算プレス）の提出を新しい順に返す。

    返す各行: {"date": "YYYY-MM-DD"（発表日）, "acc": 受理時刻の生文字列}
    日付は 8-K の reportDate（＝報告対象イベントの日＝発表日）を最優先で使う。
    EDGAR は 17:30 ET 以降に受理した提出を翌営業日付で filingDate にするため、
    filingDate をそのまま使うと引け後発表が1日後ろにズレることがある。
    """
    try:
        d = _get_json(SUBMISSIONS_URL.format(cik=cik))
    except Exception as e:
        log(f"  EDGAR CIK{cik} 取得失敗: {type(e).__name__}: {e}")
        return []
    recent = (d.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    items = recent.get("items") or []
    accs = recent.get("acceptanceDateTime") or []
    reps = recent.get("reportDate") or []
    fils = recent.get("filingDate") or []
    rows: list[dict] = []
    for i, form in enumerate(forms):
        item = (items[i] if i < len(items) else "") or ""
        # Item 2.02 = Results of Operations（決算プレスの8-K）。"12.02" 等の誤検出を避ける
        if form != "8-K" or "2.02" not in item.split(","):
            continue
        day = (reps[i] if i < len(reps) else "") or (fils[i] if i < len(fils) else "")
        rows.append({"date": (day or "")[:10],
                     "acc": (accs[i] if i < len(accs) else "") or ""})
        if len(rows) >= SAMPLES:
            break
    return rows


def earnings_facts_for(cik: str) -> dict | None:
    """1銘柄ぶんの決算プレスの実績をまとめて返す。

      {"time":"HH:MM"|None, "session":"AM"|"PM"|None, "n":件数, "spread":ブレ分,
       "dates":["YYYY-MM-DD", ...]}   ← dates は新しい順の過去の発表日

    時刻が推定できなくても dates（次回決算日の予測に使う）は返す。
    """
    rows = _recent_8k_202(cik)
    if not rows:
        return None
    times: list[str] = []
    intraday = 0
    for r in rows:
        t, is_intraday = interpret_acceptance(r["acc"])
        if t is not None:
            times.append(t.strftime("%H:%M"))
        elif is_intraday:
            intraday += 1
    if len(times) >= MIN_SAMPLES:
        out = summarize_times(times) or {}
    elif intraday >= MIN_SAMPLES:
        # 分単位は分からないが「朝に発表している」ことは分かる（PEP 等）。
        # 時刻は呼び出し側の寄り前デフォルトに任せる。
        out = {"time": None, "session": "AM", "n": intraday, "spread": None}
    else:
        out = {"time": None, "session": None, "n": 0, "spread": None}
    out["dates"] = [r["date"] for r in rows if r["date"]]
    return out


def earnings_times_for(cik: str) -> dict | None:
    """発表時刻だけが要るとき用の薄いラッパ（時刻もセッションも不明なら None）。"""
    r = earnings_facts_for(cik)
    if not r or (r.get("time") is None and r.get("session") is None):
        return None
    return r


def summarize_times(times: list[str]) -> dict | None:
    """新しい順の "HH:MM" リストから代表時刻を決める。

    ・直近の顔ぶれ（最大4件）の多数決で今のセッション（寄り前/引け後）を決め、
      それと違うセッションの古い実績は捨てる。
      ディズニーのように引け後→寄り前へ変更した銘柄で、混ぜて平均すると
      現実には存在しない時刻（昼過ぎ等）になるのを防ぐ。
    ・残りの中央値を採る（06:59/07:01 のような分単位のブレに強い）。
    """
    if not times:
        return None

    def is_am(hhmm: str) -> bool:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m) < 9 * 60 + 30

    recent = times[:4]
    session_am = sum(1 for t in recent if is_am(t)) * 2 >= len(recent)
    kept = [t for t in times if is_am(t) == session_am]
    mins = sorted(int(t[:2]) * 60 + int(t[3:]) for t in kept)
    med = mins[len(mins) // 2] if len(mins) % 2 else (mins[len(mins) // 2 - 1] + mins[len(mins) // 2]) // 2
    return {
        "time": f"{med // 60:02d}:{med % 60:02d}",
        "session": "AM" if session_am else "PM",
        "n": len(kept),
        # 代表時刻からのブレ幅（分）。大きいほど時刻が安定していない銘柄
        "spread": (mins[-1] - mins[0]) if mins else 0,
    }


def us_earnings_time_map(symbols: list[str]) -> dict[str, dict]:
    """{ティッカー: {"time","session","n","spread","dates"}} を返す。ベストエフォート。

    時刻が推定できなかった銘柄も、過去の発表日（dates）が取れていれば返す
    （fetch_earnings 側が「次回決算日の予測」に使うため）。
    """
    cik = ticker_to_cik()
    if not cik:
        return {}
    out: dict[str, dict] = {}
    miss: list[str] = []
    n_time = 0
    for sym in sorted({s.strip().upper() for s in symbols if s}):
        c = cik.get(sym)
        if not c:
            miss.append(sym)
            continue
        r = earnings_facts_for(c)
        if r:
            out[sym] = r
            if r.get("time") or r.get("session"):
                n_time += 1
        else:
            miss.append(sym)
        time.sleep(SLEEP_SEC)
    log(f"  EDGAR 8-K実績: {len(out)} 銘柄取得（うち発表時刻を推定できたもの {n_time}）"
        f" / 不明 {len(miss)} 件")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="SEC EDGAR から米国株の決算発表時刻を推定")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--symbols", default="AAPL,NVDA,KO,PG,JNJ,KMB,MCD,VZ,DIS,ZTS,HD,DG,MCO,MA")
    args = ap.parse_args()
    m = us_earnings_time_map(args.symbols.split(","))
    for sym, v in sorted(m.items()):
        t = v["time"] or "(時刻不明・セッションのみ)"
        sp = f", ブレ±{v['spread']}分" if v.get("spread") is not None else ""
        log(f"    {sym}: {t} ET [{v['session']}] (n={v['n']}{sp})")
    return 0 if m else 1


if __name__ == "__main__":
    raise SystemExit(main())
