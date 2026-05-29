from __future__ import annotations

import time

import requests


TWSE_ACTIVE_LIST_URL = "https://www.twse.com.tw/rwd/zh/ETF/activeList"


class TwseActiveEtfProvider:
    """TWSE active ETF list. This is the canonical ETF universe."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def fetch_etfs(self) -> list[dict]:
        response = self.session.get(
            TWSE_ACTIVE_LIST_URL,
            params={"response": "json", "_": int(time.time() * 1000)},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "ok":
            raise RuntimeError(f"TWSE activeList returned status={payload.get('status')}")

        fields = payload.get("fields") or []
        data = payload.get("data") or []
        code_idx = _field_index(fields, "證券代號", 0)
        name_idx = _field_index(fields, "證券簡稱", 1)
        manager_idx = _field_index(fields, "管理方式", 2)
        category_idx = _field_index(fields, "ETF分類", 3)

        rows = []
        for row in data:
            try:
                code = str(row[code_idx]).strip()
                name = str(row[name_idx]).strip()
                manager = str(row[manager_idx]).strip() if len(row) > manager_idx else ""
                category = str(row[category_idx]).strip() if len(row) > category_idx else ""
            except (IndexError, TypeError):
                continue
            if not code or not name:
                continue
            rows.append({
                "code": code,
                "name": name,
                "manager": manager,
                "color": _color_for_index(len(rows)),
                "category": category,
            })
        return rows


def _field_index(fields: list[str], name: str, fallback: int) -> int:
    try:
        return fields.index(name)
    except ValueError:
        return fallback


def _color_for_index(index: int) -> str:
    colors = [
        "#2E6FAB", "#C58A1F", "#7E4FB8", "#2E8B8B", "#D4628A",
        "#5B6D8C", "#8B6914", "#A04A8B", "#3F8A3F", "#B33333",
    ]
    return colors[index % len(colors)]
