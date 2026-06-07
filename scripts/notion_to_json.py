"""
Notion DB を読んで data/investor_events.json を再生成する。

upsert 後に呼ばれる前提。これにより、Notion 上の手動補正も JSON に反映され、
GitHub にコミットされ、最終的にアプリ側まで届く。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    NOTION_TO_CATEGORY,
    SELECT_TO_IMPORTANCE,
    NotionClient,
    get_notion_db_id,
    load_canonical_events,
    log,
    read_checkbox,
    read_date_start,
    read_rich_text,
    read_select,
    read_title,
    read_url,
    write_canonical_events,
)


def find_property_names(client: NotionClient, db_id: str) -> dict[str, str]:
    """DB スキーマからプロパティ実名を返す。notion_upsert と同じロジック。"""
    url = f"https://api.notion.com/v1/databases/{db_id}"
    data = client._request("GET", url, None)
    props = data.get("properties", {})

    def find(jp_name: str, expected_type: str) -> str:
        if jp_name in props and props[jp_name].get("type") == expected_type:
            return jp_name
        for name, meta in props.items():
            if name.strip() == jp_name and meta.get("type") == expected_type:
                return name
        raise RuntimeError(f"プロパティ '{jp_name}' (type={expected_type}) が見つかりません")

    def find_optional(jp_name: str, expected_type: str) -> str | None:
        """任意プロパティ。DBに無ければ None を返す（落とさない）。結果欄など後付けプロパティ用。"""
        if jp_name in props and props[jp_name].get("type") == expected_type:
            return jp_name
        for name, meta in props.items():
            if name.strip() == jp_name and meta.get("type") == expected_type:
                return name
        return None

    # ID は "ID" or "userDefined:ID"
    id_name = "ID"
    if id_name not in props:
        for name in props:
            if name.lower() == "id" or name.endswith(":ID") or name.endswith(":id"):
                id_name = name
                break

    return {
        "id": id_name,
        "title": find("イベント名", "title"),
        "datetime": find("発表日時", "date"),
        "country": find("国", "select"),
        "category": find("カテゴリ", "select"),
        "importance": find("重要度", "select"),
        "is_estimated": find("推定日", "checkbox"),
        "local_time": find("現地時刻", "rich_text"),
        "timezone": find("タイムゾーン", "rich_text"),
        "description": find("説明", "rich_text"),
        "source_url": find("ソースURL", "url"),
        "result": find_optional("結果", "rich_text"),
    }


def page_to_event(page: dict, name_map: dict[str, str]) -> dict | None:
    """1 Notion ページを events JSON 形式の dict に変換。"""
    props = page.get("properties", {})
    ev_id = read_rich_text(props.get(name_map["id"], {}))
    if not ev_id:
        return None

    title = read_title(props.get(name_map["title"], {}))
    datetime_local = read_date_start(props.get(name_map["datetime"], {}))
    country = read_select(props.get(name_map["country"], {}))
    category_notion = read_select(props.get(name_map["category"], {}))
    importance_select = read_select(props.get(name_map["importance"], {}))
    is_estimated = read_checkbox(props.get(name_map["is_estimated"], {}))
    timezone_str = read_rich_text(props.get(name_map["timezone"], {})) or None
    local_time_str = read_rich_text(props.get(name_map["local_time"], {})) or None
    description = read_rich_text(props.get(name_map["description"], {})) or None
    source_url = read_url(props.get(name_map["source_url"], {}))
    result_name = name_map.get("result")
    result = (read_rich_text(props.get(result_name, {})) or None) if result_name else None

    if not datetime_local:
        log(f"  skip (no datetime): {ev_id}")
        return None

    # datetime_local は Notion から ISO-8601 形式（オフセット込み）で返る想定
    try:
        local_dt = datetime.fromisoformat(datetime_local)
    except ValueError:
        log(f"  skip (datetime parse fail): {ev_id} '{datetime_local}'")
        return None

    # tz がない場合は timezone_str を信用してみる（フォールバック）
    if local_dt.tzinfo is None and timezone_str:
        try:
            from zoneinfo import ZoneInfo
            local_dt = local_dt.replace(tzinfo=ZoneInfo(timezone_str))
        except Exception:
            pass

    if local_dt.tzinfo is None:
        log(f"  skip (no tz info): {ev_id}")
        return None

    utc_dt = local_dt.astimezone(timezone.utc)

    category = NOTION_TO_CATEGORY.get(category_notion, category_notion or "EVENT")
    importance = SELECT_TO_IMPORTANCE.get(importance_select or "", 1)

    return {
        "id": ev_id,
        "title": title,
        "category": category,
        "country": country or "JP",
        "datetime_utc": utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "datetime_local": local_dt.isoformat(),
        "timezone": timezone_str or local_dt.tzname() or "Asia/Tokyo",
        "importance": importance,
        "is_estimated": is_estimated,
        "description": description,
        "source_url": source_url,
        "result": result,
    }


def main() -> int:
    db_id = get_notion_db_id()
    client = NotionClient()
    name_map = find_property_names(client, db_id)
    log(f"  プロパティ名解決済み: ID='{name_map['id']}'")

    pages = client.query_database(db_id)
    log(f"  Notion DB から {len(pages)} ページ取得")

    events: list[dict] = []
    for page in pages:
        ev = page_to_event(page, name_map)
        if ev is not None:
            events.append(ev)

    events.sort(key=lambda e: e["datetime_utc"])

    # 既存 JSON の固定メタデータ（schema_version, importance_levels, categories, description）を継承
    seed = load_canonical_events()
    out = dict(seed)
    out["events"] = events
    out["covered_years"] = sorted({int(e["datetime_utc"][:4]) for e in events})
    out["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    write_canonical_events(out)
    log(f"  data/investor_events.json に {len(events)} 件を書き出し（covered_years={out['covered_years']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
