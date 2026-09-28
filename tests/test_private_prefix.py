"""
notion_to_json.py の非公開IDフィルタの回帰テスト（ネットワーク不要）。

守りたい性質:
  1. 保有・監視銘柄の決算と、手で入れた自分用の予定（private_*）は公開JSONに出さない
  2. 「決算シーズン開始」「権利付き最終日」のような一般イベントは出す

実行:
    python -m pytest tests/test_private_prefix.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ を import できるようにする（他のテストと同じやり方）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from notion_to_json import PRIVATE_ID_PREFIXES  # noqa: E402


def test_private_ids_are_excluded():
    # 非公開にすべきID（銘柄名が入る）
    for idv in ("hold_earnings_us_AAPL", "watch_earnings_jp_7203",
                "private_yutai_cross_2026-11"):
        assert idv.startswith(PRIVATE_ID_PREFIXES), idv


def test_public_ids_are_kept():
    # 公開してよい一般イベント
    for idv in ("us_cpi_2026-05-12", "jp_kenri_last_2026-12", "earnings_season_start"):
        assert not idv.startswith(PRIVATE_ID_PREFIXES), idv
