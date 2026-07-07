"""
相場のアノマリー（季節的な経験則）を投資家カレンダーへ生成する。

アノマリー＝「節分天井・彼岸底」「セルインメイ」「サンタクロースラリー」など、
古くから言われる暦ベースの株価傾向。外部サイトを叩かず暦計算だけで作るので
Claude / AI 呼び出しなし＝別枠クレジット消費ゼロ。

仕様（fetch_earnings.py と同じ「年次自動生成＋パイプライン合流」方式）:
- covered_years（build_events_out があればそれ、無ければシード）分を毎回生成し直す
- id = anomaly_{key}_{year}（年込みで一意）
- is_estimated=true（保護対象外＝毎朝の自動更新で日付に追従、build_events では除外）
- importance=1（★☆☆。参考情報なので ★★★ ビューを汚さない）
- 出力: tmp/fetch_anomalies_out.json （events 配列を含む辞書）
  → notion_upsert.py が build_events_out にマージして Notion に upsert

実行:
    python scripts/fetch_anomalies.py            # tmp/fetch_anomalies_out.json を生成
    python scripts/fetch_anomalies.py --self-test  # 生成結果を標準出力に表示（書き込み無し）
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_canonical_events, load_tmp, write_tmp, log  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
ET = ZoneInfo("America/New_York")

# ----------------------------------------------------------------------
# アノマリー定義表
#   key        : id に使う識別子（年が付いて anomaly_{key}_{year}）
#   title      : カレンダー表示名
#   country    : "JP" / "US"
#   month, day : その年の代表日（経験則の目安。厳密な日ではなく節目）
#   tz         : "JST"(09:00 寄り付き) / "ET"(09:30 NY寄り付き)
#   description : 由来の説明（平易な日本語）
# ----------------------------------------------------------------------
ANOMALIES = [
    # --- 日本 ---
    ("jp_january_effect", "1月効果", "JP", 1, 5, "JST",
     "1月（特に小型株）は値上がりしやすいとされる経験則。新年の新規資金流入や、"
     "前年末の節税売り（損出し）一巡後の買い戻しが背景とされる。"),
    ("jp_setsubun_tenjo", "節分天井", "JP", 2, 3, "JST",
     "「節分天井・彼岸底」の天井側。2月上旬（節分ごろ）に高値をつけやすいという"
     "古くからの経験則。年明けの上昇が一服しやすい時期とされる。"),
    ("jp_higan_zoko", "彼岸底", "JP", 3, 18, "JST",
     "「節分天井・彼岸底」の底側。春の彼岸（3月中旬〜下旬）に安値をつけやすいとされる。"
     "年度末の益出し・損出し売りが一巡し、反発に向かいやすい時期。"),
    ("jp_sell_in_may", "セルインメイ", "JP", 5, 1, "JST",
     "「Sell in May（5月に売れ）」。5月〜夏にかけて株価が軟調になりやすいという経験則。"
     "原文は『Sell in May and go away, come back on St. Leger's day（9月中旬）』。"),
    ("jp_etf_bunpai", "ETF分配金捻出売り", "JP", 7, 8, "JST",
     "国内の日経平均・TOPIX連動ETFの決算が7月上旬（7/8・7/10）に集中し、"
     "運用会社が分配金の原資を作るための機械的な先物売りが出やすい。"
     "数千億円規模の需給イベントで、例年7/7〜7/10ごろは上値が重くなりやすいとされる。"),
    ("jp_natsugare", "夏枯れ相場", "JP", 8, 12, "JST",
     "夏枯れ相場。お盆前後は機関投資家の夏休みで市場参加者が減り出来高が細る。"
     "値動きが軽くなり、薄商いの中で突発材料に振れやすい。"),
    ("jp_halloween", "ハロウィン効果", "JP", 10, 31, "JST",
     "ハロウィン効果。10月末〜翌年4月は相対的に株価パフォーマンスが良いとされる"
     "（『Sell in May』の裏返し）。10月末は買い場とする見方。"),
    ("jp_toubi", "掉尾の一振", "JP", 12, 28, "JST",
     "掉尾（とうび）の一振。年末最終週に株価が上昇しやすいとされる経験則。"
     "節税売り一巡・新年への期待・大納会に向けたご祝儀相場が背景。"),
    # --- 米国 ---
    ("us_january_barometer", "1月のバロメーター", "US", 1, 2, "ET",
     "January Barometer（1月の株価で1年が決まる）＋ January Effect（小型株が1月に上昇しやすい）。"
     "米国を代表するアノマリー。"),
    ("us_september_effect", "9月効果", "US", 9, 1, "ET",
     "September Effect。9月は歴史的に米国株が最も弱い月とされる経験則。"),
    ("us_santa_rally", "サンタクロースラリー", "US", 12, 24, "ET",
     "Santa Claus Rally。12月最終5営業日〜翌年1月最初の2営業日にかけて株価が"
     "上昇しやすいとされる経験則。"),
]


def build_anomaly_event(spec: tuple, year: int) -> dict:
    """1アノマリー定義から events JSON 形式の1件を作る。"""
    key, title, country, month, day, tzkey, description = spec
    if tzkey == "JST":
        tz = JST
        tzname = "Asia/Tokyo"
        hour, minute = 9, 0          # 東京寄り付き
    else:
        tz = ET
        tzname = "America/New_York"
        hour, minute = 9, 30         # NY 寄り付き
    local_dt = datetime(year, month, day, hour, minute, tzinfo=tz)
    utc_dt = local_dt.astimezone(ZoneInfo("UTC"))
    return {
        "id": f"anomaly_{key}_{year}",
        "title": title,
        "category": "ANOMALY",
        "country": country,
        "datetime_utc": utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "datetime_local": local_dt.isoformat(),
        "timezone": tzname,
        "importance": 1,
        "is_estimated": True,   # 暦ベースの目安。build_events では除外し毎回再生成
        "description": description,
        "source_url": None,
    }


def resolve_target_years() -> list[int]:
    """build_events_out の covered_years を優先、無ければシードの covered_years。"""
    try:
        base = load_tmp("build_events_out")
        years = base.get("covered_years") if isinstance(base, dict) else None
        if years:
            return sorted(int(y) for y in years)
    except FileNotFoundError:
        pass
    seed = load_canonical_events()
    return sorted(int(y) for y in seed.get("covered_years", [2026]))


def build_all(years: list[int]) -> list[dict]:
    events: list[dict] = []
    for year in years:
        for spec in ANOMALIES:
            events.append(build_anomaly_event(spec, year))
    events.sort(key=lambda e: e["datetime_utc"])
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description="相場アノマリーを生成")
    parser.add_argument("--self-test", action="store_true",
                        help="tmp に書かず生成結果を標準出力に表示")
    args = parser.parse_args()

    years = resolve_target_years()
    events = build_all(years)
    log(f"fetch_anomalies: 対象年={years}、生成 {len(events)} 件（{len(ANOMALIES)}種 × {len(years)}年）")

    if args.self_test:
        for ev in events:
            print(f"  {ev['datetime_local']} [{ev['country']}] {ev['title']}  ({ev['id']})")
        return 0

    path = write_tmp("fetch_anomalies_out", {"events": events})
    log(f"  {path} に書き出し")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
