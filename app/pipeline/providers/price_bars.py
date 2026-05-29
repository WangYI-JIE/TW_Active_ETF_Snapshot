from __future__ import annotations

import logging
import random
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("price_bars")


class AdjPriceProvider:
    """On-demand K-line provider using the two adj_price BCD sources."""

    def __init__(self, adj_price_dir: Path | None = None) -> None:
        self.adj_price_dir = adj_price_dir or Path(__file__).resolve().parents[3] / "adj_price"
        # The BCD parsers (adj_price/) are imported lazily on first fetch, so
        # the rest of the app — and the headless daily crawler, which never
        # fetches K-line data — can run without adj_price/ being present.
        self.sources: list | None = None

    def _ensure_sources(self) -> None:
        if self.sources is not None:
            return
        if str(self.adj_price_dir) not in sys.path:
            sys.path.insert(0, str(self.adj_price_dir))
        from parse_sinopac_bcd import fetch_sinopac_bcd_rows
        from parse_sinotrade_bcd import fetch_fubon_bcd_rows

        self.sources = [
            ("sinopac", fetch_sinopac_bcd_rows),
            ("fubon", fetch_fubon_bcd_rows),
        ]

    def fetch_price_bars(self, stock_code: str, timeout: float = 20.0) -> tuple[list[dict], str]:
        self._ensure_sources()
        sources = self.sources[:]
        random.shuffle(sources)
        last_error: Exception | None = None

        for source, fetcher in sources:
            try:
                rows = fetcher(stock_code, timeout=timeout)
                bars = [_normalize_row(stock_code, row, source) for row in rows]
                logger.info("fetched %d bars for %s from %s", len(bars), stock_code, source)
                return bars, source
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("source %s failed for %s: %s", source, stock_code, exc)

        if last_error is not None:
            logger.error("all sources failed for %s", stock_code, exc_info=last_error)
            raise last_error
        logger.warning("no sources configured for %s", stock_code)
        return [], ""


def _normalize_row(stock_code: str, row: dict, source: str) -> dict:
    return {
        "stock_code": stock_code,
        "date": datetime.strptime(row["date"], "%Y/%m/%d").date().isoformat(),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": int(float(row["volume"]) * 1000),
        "source": source,
    }
