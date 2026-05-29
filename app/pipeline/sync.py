from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db import Database


class SyncPipeline:
    def __init__(self, db: Database, provider) -> None:
        self.db = db
        self.provider = provider
        self._jobs: dict[str, dict] = {}
        self._jobs_lock = threading.Lock()

    def sync_today(self) -> dict:
        today = datetime.now(_taipei_tz()).date().isoformat()
        return self.sync_date(today)

    def sync_etf_list(self) -> dict:
        started = datetime.now()
        etfs = self.provider.fetch_etfs()
        self.db.clear_generated_market_data()
        self.db.replace_etfs(etfs)
        took_ms = int((datetime.now() - started).total_seconds() * 1000)
        return {
            "ok": True,
            "etfsProcessed": len(etfs),
            "tookMs": took_ms,
        }

    def sync_date(self, date: str, etf_codes: list[str] | None = None) -> dict:
        started = datetime.now()

        etfs = self.provider.fetch_etfs()
        self.db.upsert_etfs(etfs)
        if etf_codes:
            allowed = set(etf_codes)
            etfs = [row for row in etfs if row["code"] in allowed]

        holdings_count = 0
        trades_count = 0
        warnings: list[dict] = []

        if hasattr(self.provider, "fetch_holdings"):
            holdings, warnings = self.provider.fetch_holdings(date=date, etfs=etfs)
            self.db.upsert_stocks(_stocks_from_holdings(holdings))
            holdings_count = self.db.upsert_holdings(holdings)
        else:
            stocks = self.provider.fetch_stocks()
            trades = self.provider.fetch_trades(date=date, etfs=[row["code"] for row in etfs])
            self.db.upsert_stocks(stocks)
            trades_count = self.db.upsert_trades(trades)

        warning_count = self.db.add_sync_warnings(date, warnings)
        took_ms = int((datetime.now() - started).total_seconds() * 1000)
        return {
            "ok": True,
            "date": date,
            "etfsProcessed": len(etfs),
            "holdingsUpserted": holdings_count,
            "tradesUpserted": trades_count,
            "warnings": warnings,
            "warningsInserted": warning_count,
            "tookMs": took_ms,
        }

    def backfill(self, start: str | None, end: str | None, etfs: list[str]) -> dict:
        start_date = _parse_ymd(start)
        end_date = _parse_ymd(end)
        if end_date < start_date:
            raise ValueError("`to` must be >= `from`")

        job_id = f"bf_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        dates = _date_range(start_date.isoformat(), end_date.isoformat())
        with self._jobs_lock:
            self._jobs[job_id] = {
                "ok": True,
                "jobId": job_id,
                "status": "queued",
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
                "etfs": etfs,
                "totalDates": len(dates),
                "doneDates": 0,
                "holdingsUpserted": 0,
                "tradesUpserted": 0,
                "warningsInserted": 0,
                "currentDate": None,
                "error": None,
                "startedAt": datetime.now(_taipei_tz()).isoformat(timespec="seconds"),
                "finishedAt": None,
            }

        worker = threading.Thread(
            target=self._run_backfill,
            args=(job_id, dates, etfs),
            daemon=True,
        )
        worker.start()
        return self.get_backfill_status(job_id)

    def get_backfill_status(self, job_id: str) -> dict:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job:
                return {"ok": False, "jobId": job_id, "status": "not_found"}
            return dict(job)

    def _run_backfill(self, job_id: str, dates: list[str], etfs: list[str]) -> None:
        self._update_job(job_id, status="running")
        try:
            for idx, day in enumerate(dates, start=1):
                self._update_job(job_id, currentDate=day)
                result = self.sync_date(day, etf_codes=etfs)
                self._update_job(
                    job_id,
                    doneDates=idx,
                    holdingsUpserted=self._job_value(job_id, "holdingsUpserted") + int(result.get("holdingsUpserted", 0)),
                    tradesUpserted=self._job_value(job_id, "tradesUpserted") + int(result.get("tradesUpserted", 0)),
                    warningsInserted=self._job_value(job_id, "warningsInserted") + int(result.get("warningsInserted", 0)),
                )
            self._update_job(
                job_id,
                status="done",
                currentDate=None,
                finishedAt=datetime.now(_taipei_tz()).isoformat(timespec="seconds"),
            )
        except Exception as exc:  # pragma: no cover - defensive runtime path
            self._update_job(
                job_id,
                ok=False,
                status="error",
                error=str(exc),
                finishedAt=datetime.now(_taipei_tz()).isoformat(timespec="seconds"),
            )

    def _update_job(self, job_id: str, **fields) -> None:
        with self._jobs_lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(fields)

    def _job_value(self, job_id: str, key: str) -> int:
        with self._jobs_lock:
            return int(self._jobs.get(job_id, {}).get(key, 0))


def _taipei_tz():
    try:
        return ZoneInfo("Asia/Taipei")
    except Exception:
        return timezone(timedelta(hours=8), name="Asia/Taipei")


def _stocks_from_holdings(holdings: list[dict]) -> list[dict]:
    stocks = {}
    for row in holdings:
        code = row["stock_code"]
        stocks[code] = {
            "code": code,
            "name": row["stock_name"],
            "sector": "",
        }
    return list(stocks.values())


def _parse_ymd(value: str | None):
    if not value:
        raise ValueError("Backfill requires both `from` and `to` in YYYY-MM-DD format.")
    return datetime.strptime(value, "%Y-%m-%d").date()


def _date_range(from_date: str, to_date: str) -> list[str]:
    start = datetime.strptime(from_date, "%Y-%m-%d").date()
    end = datetime.strptime(to_date, "%Y-%m-%d").date()
    out: list[str] = []
    current = start
    while current <= end:
        out.append(current.isoformat())
        current += timedelta(days=1)
    return out
