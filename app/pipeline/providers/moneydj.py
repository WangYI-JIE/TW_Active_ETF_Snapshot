from __future__ import annotations

import importlib.util
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import requests


class MoneyDjHoldingsProvider:
    """Adapter around crawlers/fund_holdings.py."""

    source = "moneydj"

    def __init__(self, crawler_path: Path | None = None, session: requests.Session | None = None) -> None:
        self.crawler_path = crawler_path or Path(__file__).resolve().parents[3] / "crawlers" / "fund_holdings.py"
        self.session = session or requests.Session()
        self._module: ModuleType | None = None

    def fetch_holdings(self, requested_date: str, etfs: list[dict]) -> tuple[list[dict], list[dict]]:
        module = self._load_module()
        holdings_rows: list[dict] = []
        warnings: list[dict] = []
        requested = _parse_date(requested_date)

        for etf in etfs:
            code = etf["code"]
            try:
                result = module.fetch_etf_holdings(code, self.session)
            except Exception as exc:
                warnings.append({"etf_code": code, "message": f"{code} holdings fetch failed: {exc}"})
                continue

            holdings_date = _normalize_date(result.get("holdings_date"))
            if holdings_date is None:
                warnings.append({"etf_code": code, "message": f"{code} holdings_date missing; holdings skipped."})
                continue

            parsed_holdings_date = _parse_date(holdings_date)
            if parsed_holdings_date and requested and parsed_holdings_date < requested:
                warnings.append({
                    "etf_code": code,
                    "message": f"{code} holdings_date={holdings_date} is older than requested_date={requested_date}.",
                })

            for item in result.get("holdings") or []:
                stock_code = str(item.get("code", "")).strip()
                stock_name = str(item.get("name", "")).strip()
                if not stock_code or not stock_name:
                    continue
                holdings_rows.append({
                    "holdings_date": holdings_date,
                    "etf_code": code,
                    "stock_code": stock_code,
                    "stock_name": stock_name,
                    "lots": int(item.get("lots") or 0),
                    "weight": float(item["weight"]) if item.get("weight") is not None else None,
                    "source": self.source,
                })

            skipped_count = len(result.get("skipped_rows") or [])
            if skipped_count:
                warnings.append({
                    "etf_code": code,
                    "message": f"{code} skipped {skipped_count} MoneyDJ holding rows during parsing.",
                })

        return holdings_rows, warnings

    def _load_module(self) -> ModuleType:
        if self._module is not None:
            return self._module
        if not self.crawler_path.exists():
            raise FileNotFoundError(self.crawler_path)
        spec = importlib.util.spec_from_file_location("fund_holdings_crawler", self.crawler_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load crawler from {self.crawler_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "fetch_etf_holdings"):
            raise AttributeError("crawlers/fund_holdings.py must define fetch_etf_holdings(etf_code, session)")
        self._module = module
        return module


def _normalize_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip().replace("/", "-")
    parsed = _parse_date(text)
    return parsed.isoformat() if parsed else None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None
