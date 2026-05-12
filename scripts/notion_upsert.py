"""
tmp/build_events_out.json + tmp/fetch_*_out.json をマージして Notion DB に upsert する。

仕様:
- ID（rich_text）をユニークキーに upsert
- 既存ページの `推定日(is_estimated)=false` の行は触らない（人が手動で確定値に補正した行を保護）
- fetch_* の出力は build_events の同 id を上書き（公式サイトの確定値を優先）
- 新規 ID は create_pages

実行:
    set NOTION_TOKEN=secret_xxxxx
    set NOTION_DB_ID=7aaf88b59b714c628e1114391f4636f3
    python scripts/notion_upsert.py
    python scripts/notion_upsert.py --dry-run    # 件数だけ表示
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CATEGORY_TO_NOTION,
    COUNTRY_TO_NOTION,
    IMPORTANCE_TO_SELECT,
    NotionClient,
    get_notion_db_id,
    load_tmp,
    log,
    prop_checkbox,
    prop_date_with_time,
    prop_rich_text,
    prop_select,
    prop_title,
    prop_url,
    read_checkbox,
    read_rich_text,
)


def find_id_property_name(client: NotionClient, db_id: str) -> tuple[dict, str]:
    """
    DBスキーマを取得して、ID 用プロパティの実名を特定する。

    Notion API では "ID" がそのまま使える DB と、MCP 経由で作成時に "userDefined:ID" になっている DB がある。
    両方をハンドルするため、UI 表示が "ID" のプロパティを探す。
    """
    url = f"https://api.notion.com/v1/databases/{db_id}"
    data = client._request("GET", url, None)
    props: dict[str, Any] = data.get("properties", {})
    # 1) ちょうど "ID" という名前があれば最優先
    if "ID" in props:
        return props, "ID"
    # 2) "userDefined:ID" 等を含む候補
    for name in props:
        if name.lower() == "id" or name.endswith(":ID") or name.endswith(":id"):
            return props, name
    raise RuntimeError(f"Notion DB スキーマに ID 用プロパティが見つかりません。実在プロパティ: {list(props.keys())}")


def find_property_by_type(props: dict, jp_name: str, expected_type: str) -> str:
    """日本語名のプロパティを取得（型一致を確認）。"""
    if jp_name in props and props[jp_name].get("type") == expected_type:
        return jp_name
    # 大文字小文字や前後空白の揺れに耐える
    for name, meta in props.items():
        if name.strip() == jp_name and meta.get("type") == expected_type:
            return name
    raise RuntimeError(f"プロパティ '{jp_name}' (type={expected_type}) が見つかりません")


def build_properties_for_event(event: dict, name_map: dict[str, str]) -> dict:
    """1イベントを Notion プロパティ dict に変換。"""
    category_notion = CATEGORY_TO_NOTION.get(event.get("category"), None)
    country_notion = COUNTRY_TO_NOTION.get(event.get("country"), None)
    importance_select = IMPORTANCE_TO_SELECT.get(int(event.get("importance", 0)), None)
    title = event.get("title") or ""
    event_id = event.get("id") or ""
    datetime_local = event.get("datetime_local")
    timezone_str = event.get("timezone")
    description = event.get("description") or ""
    source_url = event.get("source_url")
    is_estimated = bool(event.get("is_estimated", False))

    return {
        name_map["title"]: prop_title(title),
        name_map["id"]: prop_rich_text(event_id),
        name_map["datetime"]: prop_date_with_time(datetime_local, timezone_str),
        name_map["country"]: prop_select(country_notion),
        name_map["category"]: prop_select(category_notion),
        name_map["importance"]: prop_select(importance_select),
        name_map["is_estimated"]: prop_checkbox(is_estimated),
        name_map["local_time"]: prop_rich_text(datetime_local or ""),
        name_map["timezone"]: prop_rich_text(timezone_str or ""),
        name_map["description"]: prop_rich_text(description),
        name_map["source_url"]: prop_url(source_url),
    }


def collect_events() -> list[dict]:
    """build_events + fetch_* の出力をマージ。後勝ち（fetch_* が build_events を上書き）。"""
    by_id: dict[str, dict] = {}
    try:
        base = load_tmp("build_events_out")
        for ev in base.get("events", []):
            by_id[ev["id"]] = ev
        log(f"  build_events_out: {len(base.get('events', []))} 件取り込み")
    except FileNotFoundError:
        log("  build_events_out.json が見つかりません。build_events.py を先に実行してください")
        return []

    for tmp_name in ("fetch_fomc_out", "fetch_boj_out"):
        try:
            data = load_tmp(tmp_name)
            count = 0
            for ev in data.get("events", []):
                by_id[ev["id"]] = ev
                count += 1
            log(f"  {tmp_name}: {count} 件で上書き")
        except FileNotFoundError:
            log(f"  {tmp_name}.json なし（スキップ）")
    return sorted(by_id.values(), key=lambda e: e["datetime_utc"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Notion DB に events を upsert")
    parser.add_argument("--dry-run", action="store_true", help="API を叩かず件数のみ表示")
    args = parser.parse_args()

    events = collect_events()
    if not events:
        log("upsert 対象がありません")
        return 1

    db_id = get_notion_db_id()
    client = NotionClient()
    schema_props, id_prop_name = find_id_property_name(client, db_id)
    log(f"  ID プロパティ名: '{id_prop_name}'")

    # 必要なプロパティ名を全部解決
    name_map = {
        "id": id_prop_name,
        "title": find_property_by_type(schema_props, "イベント名", "title"),
        "datetime": find_property_by_type(schema_props, "発表日時", "date"),
        "country": find_property_by_type(schema_props, "国", "select"),
        "category": find_property_by_type(schema_props, "カテゴリ", "select"),
        "importance": find_property_by_type(schema_props, "重要度", "select"),
        "is_estimated": find_property_by_type(schema_props, "推定日", "checkbox"),
        "local_time": find_property_by_type(schema_props, "現地時刻", "rich_text"),
        "timezone": find_property_by_type(schema_props, "タイムゾーン", "rich_text"),
        "description": find_property_by_type(schema_props, "説明", "rich_text"),
        "source_url": find_property_by_type(schema_props, "ソースURL", "url"),
    }

    # 既存ページを ID で索引
    existing = client.query_database(db_id)
    existing_by_id: dict[str, dict] = {}
    for page in existing:
        props = page.get("properties", {})
        id_val = read_rich_text(props.get(id_prop_name, {}))
        if id_val:
            existing_by_id[id_val] = page
    log(f"  既存ページ {len(existing_by_id)} 件")

    # upsert ループ
    created = 0
    updated = 0
    protected = 0
    skipped = 0

    for ev in events:
        ev_id = ev["id"]
        existing_page = existing_by_id.get(ev_id)
        target_props = build_properties_for_event(ev, name_map)

        if existing_page:
            # is_estimated=False の行は触らない（人の手動補正を保護）
            existing_is_estimated = read_checkbox(
                existing_page.get("properties", {}).get(name_map["is_estimated"], {})
            )
            if not existing_is_estimated and ev.get("is_estimated", False):
                # 既存=確定 vs 新規=推定 → 既存（確定値）を優先して保護
                protected += 1
                continue

            if args.dry_run:
                updated += 1
            else:
                try:
                    client.update_page(existing_page["id"], target_props)
                    updated += 1
                except Exception as e:
                    log(f"  update 失敗 {ev_id}: {type(e).__name__}: {e}")
                    skipped += 1
        else:
            if args.dry_run:
                created += 1
            else:
                try:
                    client.create_page(db_id, target_props)
                    created += 1
                except Exception as e:
                    log(f"  create 失敗 {ev_id}: {type(e).__name__}: {e}")
                    skipped += 1

    mode = "dry-run" if args.dry_run else "実行"
    log(f"upsert 完了 ({mode}): create={created}, update={updated}, protected={protected}, skipped={skipped}")
    log(f"完了時刻: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
