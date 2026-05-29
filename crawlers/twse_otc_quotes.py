from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests
from urllib3.exceptions import InsecureRequestWarning

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import Database

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
OTC_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"


def parse_date_arg(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_number(value: Any) -> float | None:
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "---", "----", "除權息", "N/A"}:
        return None
    text = text.replace("X", "")
    try:
        return float(text)
    except ValueError:
        return None


def parse_volume(value: Any) -> int | None:
    number = parse_number(value)
    return None if number is None else int(number)


def fetch_twse_payload(trade_date: date, timeout: float = 30.0) -> dict[str, Any]:
    response = requests.get(
        TWSE_URL,
        params={"date": trade_date.strftime("%Y%m%d"), "type": "ALLNOTIND", "response": "json", "_": int(time.time() * 1000)},
        headers={"Referer": "https://www.twse.com.tw/zh/trading/historical/mi-index.html"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("stat", "")).strip() != "OK":
        raise RuntimeError(f"TWSE stat={payload.get('stat')}")
    return payload


def fetch_otc_payload(trade_date: date, timeout: float = 30.0) -> dict[str, Any]:
    response = requests.post(
        OTC_URL,
        data={"date": trade_date.strftime("%Y/%m/%d"), "type": "AL", "id": "", "response": "json"},
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://www.tpex.org.tw",
            "Referer": "https://www.tpex.org.tw/zh-tw/mainboard/trading/info/mi-pricing.html",
        },
        timeout=timeout,
        verify=False,
    )
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("stat", "")).strip().lower() != "ok":
        raise RuntimeError(f"OTC stat={payload.get('stat')}")
    return payload


def extract_by_fields(row: list[Any], fields: list[str], name: str, fallback_index: int | None = None) -> Any:
    try:
        return row[fields.index(name)]
    except (ValueError, IndexError):
        if fallback_index is None:
            return None
        try:
            return row[fallback_index]
        except IndexError:
            return None


def apply_twse_sign(value: Any, sign: Any) -> float | None:
    parsed = parse_number(value)
    if parsed is None:
        return None
    text = str(sign or "")
    if "-" in text or "跌" in text:
        return -abs(parsed)
    return parsed


def find_twse_tables(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """All daily-close tables. MI_INDEX splits securities across several tables
    (股票 / ETF / ETN / 受益證券 …); ETFs live in a non-first table, so we must
    read every price table, not just the first match."""
    tables = []
    for table in payload.get("tables") or []:
        fields = table.get("fields") or []
        if "證券代號" in fields and "收盤價" in fields:
            tables.append(table)
    return tables


def extract_twse_quotes(payload: dict[str, Any], trade_date: date, allowed_codes: set[str]) -> list[dict[str, Any]]:
    rows = []
    for table in find_twse_tables(payload):
        fields = table.get("fields") or []
        for row in table.get("data") or []:
            code = str(extract_by_fields(row, fields, "證券代號", 0) or "").strip()
            if not code or code not in allowed_codes:
                continue
            close = parse_number(extract_by_fields(row, fields, "收盤價", 8))
            change = apply_twse_sign(extract_by_fields(row, fields, "漲跌價差", 10), extract_by_fields(row, fields, "漲跌(+/-)", 9))
            previous = close - change if close is not None and change is not None else None
            rows.append({
                "quote_date": trade_date.isoformat(),
                "code": code,
                "name": str(extract_by_fields(row, fields, "證券名稱", 1) or "").strip() or None,
                "market": "TSE",
                "open": parse_number(extract_by_fields(row, fields, "開盤價", 5)),
                "high": parse_number(extract_by_fields(row, fields, "最高價", 6)),
                "low": parse_number(extract_by_fields(row, fields, "最低價", 7)),
                "close": close,
                "change": change,
                "change_pct": None if previous in (None, 0) or change is None else change / previous * 100,
                "volume": parse_volume(extract_by_fields(row, fields, "成交股數", 2)),
                "source": "twse",
            })
    return rows


def find_otc_tables(payload: dict[str, Any]) -> list[dict[str, Any]]:
    all_tables = payload.get("tables") or []
    priced = [t for t in all_tables if "收盤" in (t.get("fields") or [])]
    # Fall back to the first table (legacy behaviour) if no field-tagged match.
    return priced or all_tables[:1]


def extract_otc_quotes(payload: dict[str, Any], trade_date: date, allowed_codes: set[str]) -> list[dict[str, Any]]:
    rows = []
    for table in find_otc_tables(payload):
        fields = table.get("fields") or []
        for row in table.get("data") or []:
            code = str(extract_by_fields(row, fields, "代號", 0) or "").strip()
            if not code or code not in allowed_codes:
                continue
            close = parse_number(extract_by_fields(row, fields, "收盤", 2))
            change = parse_number(extract_by_fields(row, fields, "漲跌", 3))
            previous = close - change if close is not None and change is not None else None
            rows.append({
                "quote_date": trade_date.isoformat(),
                "code": code,
                "name": str(extract_by_fields(row, fields, "名稱", 1) or "").strip() or None,
                "market": "OTC",
                "open": parse_number(extract_by_fields(row, fields, "開盤", 4)),
                "high": parse_number(extract_by_fields(row, fields, "最高", 5)),
                "low": parse_number(extract_by_fields(row, fields, "最低", 6)),
                "close": close,
                "change": change,
                "change_pct": None if previous in (None, 0) or change is None else change / previous * 100,
                "volume": parse_volume(extract_by_fields(row, fields, "成交股數", 7)),
                "source": "otc",
            })
    return rows


def fetch_quotes_for_date(trade_date: date, codes: list[str], timeout: float = 30.0) -> tuple[list[dict[str, Any]], list[str]]:
    allowed = set(codes)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        rows.extend(extract_twse_quotes(fetch_twse_payload(trade_date, timeout), trade_date, allowed))
    except Exception as exc:
        errors.append(f"TWSE {trade_date}: {exc}")
    try:
        rows.extend(extract_otc_quotes(fetch_otc_payload(trade_date, timeout), trade_date, allowed))
    except Exception as exc:
        errors.append(f"OTC {trade_date}: {exc}")
    return rows, errors


def sync_quotes_for_date(trade_date: date, db_path: Path, codes: list[str] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    db = Database(db_path)
    db.migrate()
    target_codes = sorted(set(codes or db.list_price_target_codes(trade_date.isoformat())))
    rows, errors = fetch_quotes_for_date(trade_date, target_codes, timeout)
    return {
        "date": trade_date.isoformat(),
        "targetCodes": len(target_codes),
        "fetchedRows": len(rows),
        "upsertedRows": db.upsert_daily_quotes(rows),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync TWSE/OTC daily quotes for selected ETF/holding codes.")
    parser.add_argument("--date", type=parse_date_arg, default=date.today(), help="YYYY-MM-DD")
    parser.add_argument("--db-path", type=Path, default=Path("data/market.sqlite"))
    parser.add_argument("--codes", default="", help="Optional comma-separated codes. Defaults to ETF + holdings codes in DB.")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    codes = [code.strip() for code in args.codes.split(",") if code.strip()] if args.codes else None
    json.dump(sync_quotes_for_date(args.date, args.db_path, codes=codes, timeout=args.timeout), sys.stdout, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
