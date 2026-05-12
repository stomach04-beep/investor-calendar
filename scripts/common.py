"""
共通ユーティリティ：Notion API クライアント / 環境変数読み込み / プロパティ生成。

reversal-screener/sync_to_notion.py のパターンを踏襲。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests


# ----------------------------------------------------------------------
# 定数
# ----------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
TMP_DIR = PROJECT_ROOT / "tmp"
CANONICAL_JSON = DATA_DIR / "investor_events.json"

# Notion DB: 投資家カレンダー（経済イベント）
DEFAULT_DB_ID = "7aaf88b59b714c628e1114391f4636f3"

# Notion API バージョン
NOTION_VERSION = "2022-06-28"

# レート制限：Notion API は 3 req/sec → 安全マージンで 0.4 秒間隔
RATE_LIMIT_INTERVAL = 0.4

# ID は予約語のため userDefined: プレフィックス（Notion UI 上の表示は "ID"）
ID_PROPERTY = "ID"

# JSON スキーマ：カテゴリ → Notion select 名 のマッピング（投資家カレンダーDB側）
CATEGORY_TO_NOTION = {
    "FOMC": "FOMC",
    "BOJ": "日銀",
    "JOBS": "雇用統計",
    "CPI": "CPI",
    "PCE": "PCE",
    "GDP": "GDP",
    "TANKAN": "日銀短観",
    "DIVIDEND": "配当・権利",
    "EARNINGS": "決算",
    "MARKET": "市場",
    "EVENT": "その他イベント",
}
NOTION_TO_CATEGORY = {v: k for k, v in CATEGORY_TO_NOTION.items()}

# importance int → select
IMPORTANCE_TO_SELECT = {3: "★★★", 2: "★★☆", 1: "★☆☆"}
SELECT_TO_IMPORTANCE = {v: k for k, v in IMPORTANCE_TO_SELECT.items()}

# country code → select
COUNTRY_TO_NOTION = {"US": "US", "JP": "JP"}


# ----------------------------------------------------------------------
# 標準出力の UTF-8 化（Windows cp932 対策）
# ----------------------------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


def log(msg: str) -> None:
    """stderr に進捗ログを出す。"""
    print(msg, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------
# 環境変数の読み込み
# ----------------------------------------------------------------------
def _clean_secret(value: str | None) -> str:
    """BOM/前後空白除去（feedback_secret_bom_strip.md の教訓）。"""
    if not value:
        return ""
    return value.lstrip("﻿").strip()


def get_notion_token() -> str:
    """NOTION_TOKEN 環境変数を BOM 除去して返す。未設定なら例外。"""
    token = _clean_secret(os.environ.get("NOTION_TOKEN"))
    if not token:
        raise RuntimeError("NOTION_TOKEN 環境変数が未設定（または BOM/空白除去後に空）")
    return token


def get_notion_db_id() -> str:
    """NOTION_DB_ID 環境変数。未設定ならデフォルト DB を返す。"""
    db_id = _clean_secret(os.environ.get("NOTION_DB_ID"))
    return db_id or DEFAULT_DB_ID


# ----------------------------------------------------------------------
# Notion API クライアント
# ----------------------------------------------------------------------
class NotionClient:
    """Notion API の薄いラッパー。レート制限・リトライ込み。"""

    def __init__(self, token: str | None = None):
        token = token or get_notion_token()
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self._last_request = 0.0

    def _wait(self) -> None:
        """レート制限間隔を確保する。"""
        elapsed = time.time() - self._last_request
        if elapsed < RATE_LIMIT_INTERVAL:
            time.sleep(RATE_LIMIT_INTERVAL - elapsed)
        self._last_request = time.time()

    def _request(self, method: str, url: str, payload: dict | None = None, max_retries: int = 4) -> dict:
        last_err: Exception | None = None
        body = json.dumps(payload, allow_nan=False, ensure_ascii=False) if payload is not None else None
        for attempt in range(max_retries):
            self._wait()
            try:
                if method == "POST":
                    r = self.session.post(url, data=body, timeout=60)
                elif method == "PATCH":
                    r = self.session.patch(url, data=body, timeout=60)
                else:
                    r = self.session.get(url, timeout=60)
            except requests.exceptions.RequestException as e:
                last_err = e
                wait = 2 ** attempt
                log(f"  [{type(e).__name__}] {wait}秒後にリトライ ({attempt+1}/{max_retries})")
                time.sleep(wait)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 5)) + 1
                log(f"  [429] {wait}秒待機 ({attempt+1}/{max_retries})")
                time.sleep(wait)
                continue
            if 500 <= r.status_code < 600:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Notion API {r.status_code} {method} {url}: {r.text[:300]}")
        if last_err:
            raise RuntimeError(f"max retries {method} {url}: {type(last_err).__name__}: {last_err}")
        raise RuntimeError(f"max retries {method} {url}")

    def query_database(self, database_id: str, filter_obj: dict | None = None) -> list[dict]:
        """DB の全ページを取得（ページネーション吸収）。"""
        url = f"https://api.notion.com/v1/databases/{database_id}/query"
        results: list[dict] = []
        cursor: str | None = None
        while True:
            payload: dict[str, Any] = {"page_size": 100}
            if filter_obj:
                payload["filter"] = filter_obj
            if cursor:
                payload["start_cursor"] = cursor
            data = self._request("POST", url, payload)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results

    def create_page(self, database_id: str, properties: dict) -> dict:
        url = "https://api.notion.com/v1/pages"
        return self._request("POST", url, {
            "parent": {"database_id": database_id},
            "properties": properties,
        })

    def update_page(self, page_id: str, properties: dict) -> dict:
        url = f"https://api.notion.com/v1/pages/{page_id}"
        return self._request("PATCH", url, {"properties": properties})


# ----------------------------------------------------------------------
# Notion プロパティ生成ヘルパー
# ----------------------------------------------------------------------
def prop_title(value: str) -> dict:
    return {"title": [{"text": {"content": value or ""}}]}


def prop_rich_text(value: str | None) -> dict:
    return {"rich_text": [{"text": {"content": value or ""}}]}


def prop_select(value: str | None) -> dict:
    if not value:
        return {"select": None}
    return {"select": {"name": value}}


def prop_checkbox(value: bool) -> dict:
    return {"checkbox": bool(value)}


def prop_url(value: str | None) -> dict:
    return {"url": value if value else None}


def prop_date_with_time(iso_local: str | None, timezone_str: str | None = None) -> dict:
    """
    日付プロパティ。datetime_local（オフセット付き）+ timezone_str を渡すと
    Notion 側で TZ 込みで保持される。

    Notion API 仕様：`time_zone` を明示するなら、`start` の末尾オフセット（+HH:MM/-HH:MM/Z）は
    取り除いて「naive ISO 形式」で送る必要がある。
    （400 "if time zone is explicitly provided, start and end can't have non-zero time offsets from UTC"）
    """
    if not iso_local:
        return {"date": None}
    start = iso_local
    if timezone_str:
        import re as _re
        start = _re.sub(r"([+-]\d{2}:\d{2}|Z)$", "", start)
    payload: dict[str, Any] = {"date": {"start": start}}
    if timezone_str:
        payload["date"]["time_zone"] = timezone_str
    return payload


# ----------------------------------------------------------------------
# 逆方向：Notion プロパティ → 値
# ----------------------------------------------------------------------
def read_title(prop: dict) -> str:
    arr = prop.get("title") or []
    return "".join(seg.get("plain_text", "") for seg in arr)


def read_rich_text(prop: dict) -> str:
    arr = prop.get("rich_text") or []
    return "".join(seg.get("plain_text", "") for seg in arr)


def read_select(prop: dict) -> str | None:
    sel = prop.get("select")
    if not sel:
        return None
    return sel.get("name")


def read_checkbox(prop: dict) -> bool:
    return bool(prop.get("checkbox"))


def read_url(prop: dict) -> str | None:
    return prop.get("url")


def read_date_start(prop: dict) -> str | None:
    date = prop.get("date")
    if not date:
        return None
    return date.get("start")


# ----------------------------------------------------------------------
# 既存 JSON の読み書き
# ----------------------------------------------------------------------
def load_canonical_events() -> dict:
    """data/investor_events.json を読み込む。"""
    with CANONICAL_JSON.open(encoding="utf-8") as f:
        return json.load(f)


def write_canonical_events(data: dict) -> None:
    """data/investor_events.json に書き込む。"""
    CANONICAL_JSON.parent.mkdir(parents=True, exist_ok=True)
    with CANONICAL_JSON.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_tmp(name: str) -> dict | list:
    """tmp/<name>.json を読み込む。"""
    p = TMP_DIR / f"{name}.json"
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def write_tmp(name: str, data: Any) -> Path:
    """tmp/<name>.json に書き込む。"""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    p = TMP_DIR / f"{name}.json"
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return p
