from __future__ import annotations

import argparse
import csv
import json
import re
import ssl
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SECTION_NAMES = ("date", "open", "high", "low", "close", "volume")
DATE_PATTERN = re.compile(r"^\d{4}/\d{2}/\d{2}$")
FUBON_BCD_BASE_URL = "https://fubon-ebrokerdj.fbs.com.tw/z/BCD/czkc1.djbcd"
DEFAULT_REFERER = "https://fubon-ebrokerdj.fbs.com.tw/"


def parse_bcd_payload(text: str) -> list[dict[str, str]]:
    sections = re.split(r"\s+", text.strip())
    if len(sections) != len(SECTION_NAMES):
        raise ValueError(
            f"Expected {len(SECTION_NAMES)} sections, got {len(sections)}. "
            "The payload should be: date open high low close volume."
        )

    columns: dict[str, list[str]] = {}
    expected_count: int | None = None

    for name, section in zip(SECTION_NAMES, sections):
        values = [item.strip() for item in section.split(",") if item.strip()]
        if expected_count is None:
            expected_count = len(values)
        elif len(values) != expected_count:
            raise ValueError(
                f"Section '{name}' has {len(values)} values, expected {expected_count}."
            )
        columns[name] = values

    if not columns["date"]:
        return []

    invalid_dates = [value for value in columns["date"] if not DATE_PATTERN.match(value)]
    if invalid_dates:
        raise ValueError(f"Invalid date values found: {invalid_dates[:3]}")

    rows: list[dict[str, str]] = []
    for i in range(len(columns["date"])):
        rows.append({name: columns[name][i] for name in SECTION_NAMES})

    return rows


def fetch_url(
    url: str,
    referer: str | None = None,
    cookie: str | None = None,
    timeout: float = 20.0,
) -> str:
    headers = {
        "Accept": "*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7,zh-CN;q=0.6",
        "Cache-Control": "max-age=0",
        "Content-Type": "text/html;charset=big5",
        "If-Modified-Since": "Wed, 15 Nov 1995 04:58:08 GMT",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
    }
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie

    request = Request(url, headers=headers)
    context = ssl._create_unverified_context()
    with urlopen(request, timeout=timeout, context=context) as response:
        payload = response.read()
        content_type = response.headers.get_content_charset()

    for encoding in (content_type, "big5", "utf-8"):
        if not encoding:
            continue
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue

    return payload.decode("latin-1")


def fetch_bcd_rows(
    url: str,
    referer: str | None = None,
    cookie: str | None = None,
    timeout: float = 20.0,
) -> list[dict[str, str]]:
    text = fetch_url(url, referer=referer, cookie=cookie, timeout=timeout)
    return parse_bcd_payload(text)


def build_fubon_bcd_url(
    stock_id: str,
    market: str = "A",
    compare_stock_id: str = "2880",
    e: int = 1,
    ver: int = 5,
) -> str:
    query = urlencode(
        {
            "a": stock_id,
            "b": market,
            "c": compare_stock_id,
            "E": e,
            "ver": ver,
        }
    )
    return f"{FUBON_BCD_BASE_URL}?{query}"


def fetch_fubon_bcd_rows(
    stock_id: str,
    cookie: str | None = None,
    market: str = "A",
    compare_stock_id: str = "2880",
    e: int = 1,
    ver: int = 5,
    referer: str = DEFAULT_REFERER,
    timeout: float = 20.0,
) -> list[dict[str, str]]:
    url = build_fubon_bcd_url(
        stock_id=stock_id,
        market=market,
        compare_stock_id=compare_stock_id,
        e=e,
        ver=ver,
    )
    return fetch_bcd_rows(url, referer=referer, cookie=cookie, timeout=timeout)


def write_csv_stdout(rows: list[dict[str, str]]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=SECTION_NAMES)
    writer.writeheader()
    writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and parse Fubon DJ BCD payload.")
    parser.add_argument("stock_id", help="Target stock id, for example 2881.")
    parser.add_argument(
        "--cookie",
        help="Optional Cookie header when fetching from URL.",
    )
    parser.add_argument(
        "--market",
        default="A",
        help="Query param b. Defaults to A.",
    )
    parser.add_argument(
        "--compare-stock-id",
        default="2880",
        help="Query param c. Defaults to 2880.",
    )
    parser.add_argument(
        "--e",
        type=int,
        default=1,
        help="Query param E. Defaults to 1.",
    )
    parser.add_argument(
        "--ver",
        type=int,
        default=5,
        help="Query param ver. Defaults to 5.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
        help="Output format. Defaults to json.",
    )
    args = parser.parse_args()

    rows = fetch_fubon_bcd_rows(
        stock_id=args.stock_id,
        cookie=args.cookie,
        market=args.market,
        compare_stock_id=args.compare_stock_id,
        e=args.e,
        ver=args.ver,
    )

    if args.format == "csv":
        write_csv_stdout(rows)
    else:
        json.dump(rows, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")

    print(f"parsed_rows={len(rows)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
