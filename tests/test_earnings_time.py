"""
決算イベントの「日時の正確さ」まわりの回帰テスト（ネットワーク不要）。

守りたい性質:
  1. 日本株の時刻は 15:00 決め打ちでなく、銘柄ごとの開示実績（J-Quants DiscTime）を使う
     （実績では 15:00 ちょうどは全開示の 7% しかない。45% は 15:30）
  2. 米国株の時刻は SEC EDGAR の 8-K(2.02) 受理時刻の実績を使う。
     ただし acceptanceDateTime は UTC 表記と ET 表記が混在するため、
     「決算プレスとしてあり得る時刻か」で解釈を選ぶ
  3. プレスから数時間遅れて 8-K を出す銘柄（PEP）で誤った時刻を採用しない
     ＝時刻は諦めてセッション（寄り前）だけ拾う
  4. 引け後→寄り前へ変更した銘柄（DIS）で、新旧を混ぜた中途半端な時刻を作らない
  5. 実績が無いときは従来どおりの既定値（日本 15:00 / 米国 07:00・16:00 ET）に落ちる

実行:
    pip install -r requirements-dev.txt
    python -m pytest tests/test_earnings_time.py -v
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_earnings as fe  # noqa: E402
import us_earnings_time as ue  # noqa: E402


# ----------------------------------------------------------------------
# 1. acceptanceDateTime の解釈（UTC 表記と ET 表記の混在）
# ----------------------------------------------------------------------
def test_utc_interpretation_matches_known_release():
    """AAPL 2026-07-30T20:30:28Z は 16:30 ET（実際の発表時刻）と解釈される。"""
    t, intraday = ue.interpret_acceptance("2026-07-30T20:30:28.000Z")
    assert t is not None and t.strftime("%H:%M") == "16:30"
    assert intraday is False


def test_et_interpretation_when_utc_is_impossible():
    """JNJ 2026-07-15T07:49:04Z は UTC 解釈だと 03:49 ET（提出不可能な時刻）。
    そのまま ET と解釈して 07:49 を採る。"""
    t, _ = ue.interpret_acceptance("2026-07-15T07:49:04.000Z")
    assert t is not None and t.strftime("%H:%M") == "07:49"


def test_intraday_filing_is_not_used_as_release_time():
    """PEP 2026-04-16T18:01:12Z は UTC 解釈で 14:01 ET＝場中。
    発表時刻としては使わず「朝に発表した」票としてだけ扱う。"""
    t, intraday = ue.interpret_acceptance("2026-04-16T18:01:12.000Z")
    assert t is None
    assert intraday is True


def test_late_evening_is_rejected():
    """17:30 を過ぎた ET 解釈は決算プレスの時刻として採らない（誤採用の防止）。"""
    t, intraday = ue.interpret_acceptance("2026-02-03T18:12:16.000Z")  # ET解釈=18:12
    assert t is None
    assert intraday is True  # UTC解釈=13:12＝場中


# ----------------------------------------------------------------------
# 2. 代表時刻のまとめ方
# ----------------------------------------------------------------------
def test_summarize_uses_median_not_outlier():
    """分単位のブレは中央値で吸収する。"""
    r = ue.summarize_times(["06:59", "07:01", "07:00", "07:02", "06:58"])
    assert r["time"] == "07:00"
    assert r["session"] == "AM"


def test_summarize_drops_old_session_after_regime_change():
    """引け後→寄り前に変更した銘柄で、古い引け後の実績を混ぜない。"""
    r = ue.summarize_times(["06:40", "06:45", "06:42", "06:44", "16:05", "16:02"])
    assert r["session"] == "AM"
    assert r["time"].startswith("06:")
    assert r["n"] == 4  # 引け後の2件は捨てられている


def test_summarize_keeps_after_hours_stock():
    r = ue.summarize_times(["16:30", "16:30", "16:31", "16:29"])
    assert r["session"] == "PM"
    assert r["time"] == "16:30"


# ----------------------------------------------------------------------
# 3. 時刻のイベントへの反映
# ----------------------------------------------------------------------
def _jp(**kw):
    base = {"name": "任天堂", "ticker": "7974", "market": "日本"}
    base.update(kw)
    return base


def _us(**kw):
    base = {"name": "Apple", "ticker": "AAPL", "market": "米国"}
    base.update(kw)
    return base


def test_jp_uses_actual_disclosure_time():
    ev = fe.build_event(_jp(time_hhmm="15:30"), date(2026, 8, 6))
    assert ev["datetime_local"] == "2026-08-06T15:30:00+09:00"


def test_jp_falls_back_to_default_time():
    ev = fe.build_event(_jp(time_hhmm=None), date(2026, 8, 6))
    assert ev["datetime_local"] == "2026-08-06T15:00:00+09:00"


def test_us_uses_actual_release_time_with_dst_offset():
    ev = fe.build_event(_us(time_hhmm="16:30", session="PM"), date(2026, 10, 30))
    assert ev["datetime_local"] == "2026-10-30T16:30:00-04:00"   # 夏時間
    ev2 = fe.build_event(_us(time_hhmm="16:30", session="PM"), date(2026, 12, 10))
    assert ev2["datetime_local"] == "2026-12-10T16:30:00-05:00"  # 冬時間


def test_us_falls_back_to_session_defaults():
    """時刻不明でも寄り前と分かっていれば 07:00 ET（従来の 16:00 決め打ちを避ける）。"""
    am = fe.build_event(_us(time_hhmm=None, session="AM"), date(2026, 10, 8))
    assert am["datetime_local"] == "2026-10-08T07:00:00-04:00"
    unknown = fe.build_event(_us(time_hhmm=None, session=None), date(2026, 10, 8))
    assert unknown["datetime_local"] == "2026-10-08T16:00:00-04:00"


def test_broken_time_string_falls_back():
    ev = fe.build_event(_jp(time_hhmm="おかしな値"), date(2026, 8, 6))
    assert ev["datetime_local"] == "2026-08-06T15:00:00+09:00"


# ----------------------------------------------------------------------
# 4. 日本株の開示時刻マップ（実ファイルの健全性）
# ----------------------------------------------------------------------
def test_jq_disc_time_map_is_present_and_well_formed():
    """jq_earnings_jp.json に disc_times があり、HH:MM 形式で入っていること。
    （jquants-bulk/build_earnings_estimates.py を再生成し忘れると空になる）"""
    m = fe.jq_disc_time_map()
    assert len(m) > 3000, "銘柄数が少なすぎる＝再生成漏れの疑い"
    assert all(re.fullmatch(r"\d{2}:\d{2}", v) for v in m.values())
    # 15:00 決め打ちに戻っていないこと（実績では 15:30 が最多）
    assert m.get("7974") == "15:30"


def test_jq_payload_has_both_dates_and_times():
    payload = json.loads(fe.JQ_ESTIMATES_PATH.read_text(encoding="utf-8"))
    assert payload.get("estimates"), "決算予測日が消えている"
    assert payload.get("disc_times"), "開示時刻が消えている"
