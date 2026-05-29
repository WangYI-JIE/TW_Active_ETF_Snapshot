from __future__ import annotations

import logging
from pathlib import Path

from app.db import Database
from app.pipeline.providers.active_etf import ActiveEtfProvider
from app.pipeline.providers.price_bars import AdjPriceProvider
from app.pipeline.sync import SyncPipeline
from crawlers.twse_otc_quotes import parse_date_arg, sync_quotes_for_date

logger = logging.getLogger("api")


class AppApi:
    """Methods exposed to JavaScript through window.pywebview.api."""

    def __init__(self, db_path: Path) -> None:
        self.db = Database(db_path)
        self.db.migrate()
        self.pipeline = SyncPipeline(self.db, ActiveEtfProvider())
        self.price_provider = AdjPriceProvider()

    def health(self) -> dict:
        return {"ok": True, "dbPath": str(self.db.path)}

    def get_etfs(self) -> list[dict]:
        return self.db.list_etfs()

    def get_stocks(self) -> list[dict]:
        return self.db.list_stocks()

    def get_trades(self, date: str, etfs: list[str] | None = None) -> list[dict]:
        return self.db.list_trades(date=date, etfs=etfs or [])

    def get_holdings(self, holdings_date: str, etfs: list[str] | None = None) -> list[dict]:
        return self.db.list_holdings(holdings_date=holdings_date, etfs=etfs or [])

    def get_holding_dates(self) -> list[str]:
        return self.db.list_holding_dates()

    def get_latest_holding_date(self) -> str | None:
        return self.db.latest_holding_date()

    def get_price_bars(self, stock_code: str, refresh: bool = False, limit: int | None = None) -> dict:
        existing = [] if refresh else self.db.list_price_bars(stock_code, limit=limit)
        if existing:
            return {"ok": True, "stockCode": stock_code, "source": "sqlite", "bars": existing}

        try:
            rows, source = self.price_provider.fetch_price_bars(stock_code)
        except Exception as exc:  # noqa: BLE001
            logger.error("get_price_bars failed for %s: %s", stock_code, exc, exc_info=exc)
            return {"ok": False, "stockCode": stock_code, "source": "", "bars": [], "error": str(exc)}

        self.db.ensure_stock(stock_code)
        self.db.upsert_price_bars(rows)
        bars = self.db.list_price_bars(stock_code, limit=limit)
        if not bars:
            logger.warning("get_price_bars returned 0 bars for %s (source=%s)", stock_code, source)
        return {
            "ok": True,
            "stockCode": stock_code,
            "source": source,
            "bars": bars,
        }

    def get_holdings_diff(self, date: str, prev_date: str, etfs: list[str] | None = None) -> list[dict]:
        """Return trade-like change records derived from two holdings snapshots."""
        return self.db.compute_holdings_diff(date=date, prev_date=prev_date, etfs=etfs or [])

    def get_trades_for_stock(self, stock_code: str, from_date: str, to_date: str, etfs: list[str] | None = None) -> list[dict]:
        return self.db.list_trades_range(stock_code=stock_code, from_date=from_date, to_date=to_date, etfs=etfs or [])

    def get_stock_changes_range(self, stock_code: str, from_date: str, to_date: str, etfs: list[str] | None = None) -> list[dict]:
        return self.db.list_stock_changes_range(stock_code=stock_code, from_date=from_date, to_date=to_date, etfs=etfs or [])

    def get_sync_warnings(self, date: str) -> list[dict]:
        return self.db.list_sync_warnings(date)

    def get_daily_quotes(self, quote_date: str, codes: list[str] | None = None) -> list[dict]:
        return self.db.list_daily_quotes(quote_date, codes)

    def sync_daily_quotes(self, quote_date: str, codes: list[str] | None = None) -> dict:
        return sync_quotes_for_date(parse_date_arg(quote_date), self.db.path, codes=codes)

    def sync_etf_list(self) -> dict:
        return self.pipeline.sync_etf_list()

    def sync_today(self) -> dict:
        result = self.pipeline.sync_today()
        # Sync quotes for the latest actual holdings date (MoneyDJ may lag 1-2 days),
        # not for result["date"] (today) which has no stock holdings yet.
        latest_date = self.db.latest_holding_date() or result["date"]
        quote_result = sync_quotes_for_date(parse_date_arg(latest_date), self.db.path, codes=None)
        result["quotesDate"] = latest_date
        result["quotes"] = quote_result
        return result

    def export_holdings_diff_csv(self, date: str, prev_date: str, etfs: list[str] | None = None) -> dict:
        """Export the constituents-change view (holdings diff) for a date to CSV."""
        from app.export import write_dicts_csv

        rows = self.db.compute_holdings_diff(date=date, prev_date=prev_date, etfs=etfs or [])
        columns = ["date", "etf_code", "etf_name", "stock_code", "stock_name",
                   "action", "shares", "price", "value"]
        out = self.db.path.parent / "exports" / f"holdings_diff_{date}.csv"
        try:
            write_dicts_csv(rows, out, columns)
        except Exception as exc:  # noqa: BLE001
            logger.error("export_holdings_diff_csv failed: %s", exc, exc_info=exc)
            return {"ok": False, "error": str(exc)}
        logger.info("exported %d holdings-diff rows to %s", len(rows), out)
        return {"ok": True, "path": str(out), "rows": len(rows)}

    def export_price_bars_csv(self, stock_code: str) -> dict:
        """Export a stock's K-line bars to CSV."""
        from app.export import write_dicts_csv

        rows = self.db.list_price_bars(stock_code)
        columns = ["date", "open", "high", "low", "close", "volume", "source"]
        out = self.db.path.parent / "exports" / f"price_bars_{stock_code}.csv"
        try:
            write_dicts_csv(rows, out, columns)
        except Exception as exc:  # noqa: BLE001
            logger.error("export_price_bars_csv failed: %s", exc, exc_info=exc)
            return {"ok": False, "error": str(exc)}
        logger.info("exported %d price bars for %s to %s", len(rows), stock_code, out)
        return {"ok": True, "path": str(out), "rows": len(rows)}

    def backfill(self, payload: dict) -> dict:
        return self.pipeline.backfill(
            start=payload.get("from"),
            end=payload.get("to"),
            etfs=payload.get("etfs") or [],
        )

    def get_backfill_status(self, job_id: str) -> dict:
        return self.pipeline.get_backfill_status(job_id)
