"""
イベント種別ごとの「公表時刻・タイムゾーン・営業日の決まり」を1か所にまとめた規則表。

なぜ必要か（2026-10-30）:
  日本GDP・日銀短観のシードが全件「前日 23:50 JST」（正しくは 8:50 JST）になっていた。
  8:50 JST を UTC へ直した値をさらに JST として書いた二重がけで、日付まで1日ずれる。
  時刻を各スクリプトや手入力に散らすと、こうしたずれを誰も検知できない。
  → 時刻の決まりはここだけに書き、生成側（fetch_jp_schedules など）も
    検査側（check_release_rules）もこの表を読む（同じ値を2か所に書かない）。

使い方:
  rule = rule_for(event)             # 当てはまる規則（無ければ None）
  local_iso, utc_z = at_rule_time(rule, date(2026, 11, 16))
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

JP_HOLIDAYS_PATH = Path(__file__).resolve().parents[1] / "data" / "jp_market_holidays.json"


@dataclass(frozen=True)
class ReleaseRule:
    """1種類のイベントの公表時刻の決まり。"""
    key: str            # 規則の名前（ログ・警告に出す）
    label: str          # 日本語の呼び名
    tz: str             # IANA タイムゾーン（現地時刻はこのタイムゾーンで持つ）
    hour: int
    minute: int
    market: str         # 営業日判定に使う市場 "JP" / "US"
    open_day: bool = True   # 土日・休場日に乗ったら誤りとみなすか

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


# ----------------------------------------------------------------------
# 規則表（id の頭の文字列 → 規則）
#   長い頭から先に照合する（us_fomc_minutes_ を us_fomc_ より先に見る）。
#   時刻が回ごとに変わるもの（日銀会合の結果・EIA の祝日週・決算など）は載せない。
# ----------------------------------------------------------------------
_RULES: list[tuple[str, ReleaseRule]] = [
    # 日本
    ("jp_gdp_", ReleaseRule("jp_gdp", "日本GDP（内閣府）", "Asia/Tokyo", 8, 50, "JP")),
    ("jp_tankan_", ReleaseRule("jp_tankan", "日銀短観", "Asia/Tokyo", 8, 50, "JP")),
    ("jp_cpi_", ReleaseRule("jp_cpi", "全国CPI（総務省）", "Asia/Tokyo", 8, 30, "JP")),
    ("jp_exdiv_", ReleaseRule("jp_exdiv", "権利付き最終日（大引け）", "Asia/Tokyo", 15, 30, "JP")),
    ("jp_market_okatsu_", ReleaseRule("jp_okatsu", "大発会（寄り付き）", "Asia/Tokyo", 9, 0, "JP")),
    ("jp_market_onoukai_", ReleaseRule("jp_onoukai", "大納会（大引け）", "Asia/Tokyo", 15, 30, "JP")),
    ("jp_majorsq_", ReleaseRule("jp_majorsq", "日メジャーSQ（寄り付き）", "Asia/Tokyo", 9, 0, "JP")),
    # 米国（いずれも米東部時間。夏冬時間は zoneinfo が自動で切り替える）
    ("us_cpi_", ReleaseRule("us_cpi", "米CPI", "America/New_York", 8, 30, "US")),
    ("us_ppi_", ReleaseRule("us_ppi", "米PPI", "America/New_York", 8, 30, "US")),
    ("us_nfp_", ReleaseRule("us_nfp", "米雇用統計", "America/New_York", 8, 30, "US")),
    ("us_pce_", ReleaseRule("us_pce", "米PCE", "America/New_York", 8, 30, "US")),
    ("us_gdp_", ReleaseRule("us_gdp", "米GDP", "America/New_York", 8, 30, "US")),
    ("us_retail_", ReleaseRule("us_retail", "米小売売上高", "America/New_York", 8, 30, "US")),
    ("us_ism_", ReleaseRule("us_ism", "ISM景況指数", "America/New_York", 10, 0, "US")),
    ("us_fomc_minutes_", ReleaseRule("us_fomc_minutes", "FOMC議事要旨", "America/New_York", 14, 0, "US")),
    ("us_fomc_", ReleaseRule("us_fomc", "FOMC声明", "America/New_York", 14, 0, "US")),
    ("us_beige_", ReleaseRule("us_beige", "ベージュブック", "America/New_York", 14, 0, "US")),
]
_RULES.sort(key=lambda kv: -len(kv[0]))

# 旧形式の id（jp_market_2026-01-05 など）は題名で大発会・大納会を見分ける
_LEGACY_MARKET = {"大発会": "jp_market_okatsu_", "大納会": "jp_market_onoukai_"}


def rule_for(event: dict) -> ReleaseRule | None:
    """イベントに当てはまる規則を返す。当てはまらなければ None。"""
    ev_id = str(event.get("id", ""))
    if ev_id.startswith("jp_market_") and not ev_id.startswith(tuple(_LEGACY_MARKET.values())):
        for word, prefix in _LEGACY_MARKET.items():
            if word in str(event.get("title", "")):
                return rule_for({"id": prefix})
        return None
    for prefix, rule in _RULES:
        if ev_id.startswith(prefix):
            return rule
    return None


def rule_by_prefix(prefix: str) -> ReleaseRule:
    """生成側が使う：id の頭から規則を引く（無ければ KeyError）。"""
    rule = rule_for({"id": prefix})
    if rule is None:
        raise KeyError(prefix)
    return rule


def at_rule_time(rule: ReleaseRule, d: date, hour: int | None = None,
                 minute: int | None = None) -> tuple[str, str]:
    """日付 d を規則の現地時刻にして (datetime_local, datetime_utc) を返す。

    現地の日付＋時刻から UTC を計算する（UTC を手で書かない＝二重がけを起こさない）。
    公式ページに時刻が書いてあるときは hour/minute で上書きできる。
    """
    local = datetime.combine(d, time(rule.hour if hour is None else hour,
                                     rule.minute if minute is None else minute),
                             tzinfo=rule.zone)
    utc = local.astimezone(timezone.utc)
    return local.isoformat(), utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def local_datetime(event: dict, rule: ReleaseRule) -> datetime:
    """イベントの datetime_utc を規則のタイムゾーンの現地時刻に直す（アプリと同じ見え方）。"""
    utc = datetime.fromisoformat(str(event["datetime_utc"]).replace("Z", "+00:00"))
    return utc.astimezone(rule.zone)


# ----------------------------------------------------------------------
# 営業日判定
# ----------------------------------------------------------------------
_jp_closed_cache: tuple[set[date], date | None] | None = None


def jp_closed_days() -> tuple[set[date], date | None]:
    """東証の休場日（平日の祝日）と、表が有効な最終日を返す。

    元データは data/jp_market_holidays.json（J-Quants の取引カレンダー由来）。
    年末年始（12/31〜1/3）は表に無くても必ず休場として扱う（東証の定め）。
    """
    global _jp_closed_cache
    if _jp_closed_cache is None:
        closed: set[date] = set()
        until: date | None = None
        try:
            payload = json.loads(JP_HOLIDAYS_PATH.read_text(encoding="utf-8"))
            closed = {date.fromisoformat(s) for s in payload.get("closed", [])}
            if payload.get("coverage_until"):
                until = date.fromisoformat(payload["coverage_until"])
        except (OSError, ValueError):
            pass
        _jp_closed_cache = (closed, until)
    return _jp_closed_cache


def is_jp_open(d: date) -> bool:
    """東証が開いている日か（土日・祝日・年末年始でない）。"""
    if d.weekday() >= 5:
        return False
    if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3):
        return False
    return d not in jp_closed_days()[0]


def jp_calendar_known(d: date) -> bool:
    """東証の休場日表がその日までカバーしているか（していなければ計算は推定扱い）。"""
    until = jp_closed_days()[1]
    return until is not None and d <= until


def is_us_open(d: date) -> bool:
    """米国の連邦祝日・土日でないか（統計の公表日の判定用）。"""
    if d.weekday() >= 5:
        return False
    from fetch_schedules import us_federal_holidays  # 遅延import（循環を避ける）
    return d not in us_federal_holidays(d.year)


def is_open(rule: ReleaseRule, d: date) -> bool:
    """規則の市場でその日が営業日か。"""
    return is_jp_open(d) if rule.market == "JP" else is_us_open(d)


def jp_shift_open_days(d: date, n: int) -> date:
    """東証の営業日で n 日ずらす（n<0 で前へ）。"""
    step = 1 if n > 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if is_jp_open(d):
            remaining -= 1
    return d
