from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("db")

# Schema version, tracked via SQLite's built-in PRAGMA user_version.
# To evolve the schema: append (next_version, "SQL ...") to _MIGRATIONS and
# bump SCHEMA_VERSION. migrate() applies only steps newer than the DB's
# current version, so existing databases upgrade in place.
SCHEMA_VERSION = 1

_V1_SCHEMA = """
                CREATE TABLE IF NOT EXISTS etfs (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    manager TEXT NOT NULL,
                    color TEXT
                );

                CREATE TABLE IF NOT EXISTS stocks (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    sector TEXT
                );

                CREATE TABLE IF NOT EXISTS trades (
                    date TEXT NOT NULL,
                    etf_code TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('new', 'exit', 'add', 'reduce')),
                    shares INTEGER NOT NULL,
                    price REAL NOT NULL,
                    value REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (date, etf_code, stock_code, action),
                    FOREIGN KEY (etf_code) REFERENCES etfs(code),
                    FOREIGN KEY (stock_code) REFERENCES stocks(code)
                );

                CREATE TABLE IF NOT EXISTS price_bars (
                    stock_code TEXT NOT NULL,
                    date TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER,
                    source TEXT NOT NULL DEFAULT 'manual',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (stock_code, date),
                    FOREIGN KEY (stock_code) REFERENCES stocks(code)
                );

                CREATE TABLE IF NOT EXISTS daily_quotes (
                    quote_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT,
                    market TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    change REAL,
                    change_pct REAL,
                    volume INTEGER,
                    source TEXT NOT NULL DEFAULT 'twse_otc',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (quote_date, code)
                );

                CREATE TABLE IF NOT EXISTS etf_holdings (
                    holdings_date TEXT NOT NULL,
                    etf_code TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    lots INTEGER NOT NULL,
                    weight REAL,
                    source TEXT NOT NULL DEFAULT 'moneydj',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (holdings_date, etf_code, stock_code),
                    FOREIGN KEY (etf_code) REFERENCES etfs(code)
                );

                CREATE TABLE IF NOT EXISTS sync_warnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT NOT NULL,
                    etf_code TEXT,
                    level TEXT NOT NULL DEFAULT 'warn',
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
"""

# (version, SQL script) applied in ascending order for any version greater
# than the database's current PRAGMA user_version.
_MIGRATIONS: list[tuple[int, str]] = [
    (1, _V1_SCHEMA),
]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def schema_version(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> None:
        with self.connect() as conn:
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            for version, script in _MIGRATIONS:
                if version > current:
                    conn.executescript(script)
                    # user_version cannot be parameter-bound; version is our own int.
                    conn.execute(f"PRAGMA user_version = {version}")
                    logger.info("applied schema migration v%d", version)
                    current = version

    def upsert_etfs(self, rows: list[dict]) -> None:
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO etfs (code, name, manager, color)
                VALUES (:code, :name, :manager, :color)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    manager = excluded.manager,
                    color = excluded.color
                """,
                rows,
            )

    def insert_missing_etfs(self, rows: list[dict]) -> None:
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO etfs (code, name, manager, color)
                VALUES (:code, :name, :manager, :color)
                ON CONFLICT(code) DO NOTHING
                """,
                rows,
            )

    def replace_etfs(self, rows: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM etfs")
            conn.executemany(
                """
                INSERT INTO etfs (code, name, manager, color)
                VALUES (:code, :name, :manager, :color)
                """,
                rows,
            )

    def clear_generated_market_data(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sync_warnings")
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM price_bars")
            conn.execute("DELETE FROM daily_quotes")
            conn.execute("DELETE FROM etf_holdings")
            conn.execute("DELETE FROM stocks")

    def upsert_stocks(self, rows: list[dict]) -> None:
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO stocks (code, name, sector)
                VALUES (:code, :name, :sector)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    sector = excluded.sector
                """,
                rows,
            )

    def ensure_stock(self, code: str, name: str | None = None, sector: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO stocks (code, name, sector)
                VALUES (?, ?, ?)
                ON CONFLICT(code) DO NOTHING
                """,
                (code, name or code, sector),
            )

    def upsert_trades(self, rows: list[dict]) -> int:
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO trades (date, etf_code, stock_code, action, shares, price, value, source)
                VALUES (:date, :etf_code, :stock_code, :action, :shares, :price, :value, :source)
                ON CONFLICT(date, etf_code, stock_code, action) DO UPDATE SET
                    shares = excluded.shares,
                    price = excluded.price,
                    value = excluded.value,
                    source = excluded.source,
                    updated_at = CURRENT_TIMESTAMP
                """,
                rows,
            )
        return len(rows)

    def upsert_holdings(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO etf_holdings (holdings_date, etf_code, stock_code, stock_name, lots, weight, source)
                VALUES (:holdings_date, :etf_code, :stock_code, :stock_name, :lots, :weight, :source)
                ON CONFLICT(holdings_date, etf_code, stock_code) DO UPDATE SET
                    stock_name = excluded.stock_name,
                    lots = excluded.lots,
                    weight = excluded.weight,
                    source = excluded.source,
                    updated_at = CURRENT_TIMESTAMP
                """,
                rows,
            )
        return len(rows)

    def upsert_price_bars(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO price_bars (stock_code, date, open, high, low, close, volume, source)
                VALUES (:stock_code, :date, :open, :high, :low, :close, :volume, :source)
                ON CONFLICT(stock_code, date) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    source = excluded.source,
                    updated_at = CURRENT_TIMESTAMP
                """,
                rows,
            )
        return len(rows)

    def upsert_daily_quotes(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO daily_quotes (
                    quote_date, code, name, market, open, high, low, close,
                    change, change_pct, volume, source
                )
                VALUES (
                    :quote_date, :code, :name, :market, :open, :high, :low, :close,
                    :change, :change_pct, :volume, :source
                )
                ON CONFLICT(quote_date, code) DO UPDATE SET
                    name = excluded.name,
                    market = excluded.market,
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    change = excluded.change,
                    change_pct = excluded.change_pct,
                    volume = excluded.volume,
                    source = excluded.source,
                    updated_at = CURRENT_TIMESTAMP
                """,
                rows,
            )
        return len(rows)

    def list_price_target_codes(self, holdings_date: str | None = None) -> list[str]:
        with self.connect() as conn:
            codes = {row["code"] for row in conn.execute("SELECT code FROM etfs")}
            if holdings_date:
                rows = conn.execute(
                    "SELECT DISTINCT stock_code AS code FROM etf_holdings WHERE holdings_date = ?",
                    (holdings_date,),
                )
            else:
                rows = conn.execute("SELECT DISTINCT stock_code AS code FROM etf_holdings")
            codes.update(row["code"] for row in rows)
        return sorted(codes)

    def list_daily_quotes(self, quote_date: str, codes: list[str] | None = None) -> list[dict]:
        params: list[object] = [quote_date]
        where = "quote_date = ?"
        if codes:
            placeholders = ",".join("?" for _ in codes)
            where += f" AND code IN ({placeholders})"
            params.extend(codes)
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM daily_quotes WHERE {where} ORDER BY code",
                    params,
                )
            ]

    def add_sync_warnings(self, run_date: str, warnings: list[dict]) -> int:
        rows = [
            {
                "run_date": run_date,
                "etf_code": warning.get("etf_code"),
                "level": warning.get("level", "warn"),
                "message": warning["message"],
            }
            for warning in warnings
        ]
        with self.connect() as conn:
            conn.execute("DELETE FROM sync_warnings WHERE run_date = ?", (run_date,))
            if not rows:
                return 0
            conn.executemany(
                """
                INSERT INTO sync_warnings (run_date, etf_code, level, message)
                VALUES (:run_date, :etf_code, :level, :message)
                """,
                rows,
            )
        return len(rows)

    def list_sync_warnings(self, run_date: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT etf_code, level, message FROM sync_warnings WHERE run_date = ? ORDER BY id",
                (run_date,),
            )
            return [dict(row) for row in rows]

    def list_etfs(self) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM etfs ORDER BY code")]

    def list_stocks(self) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM stocks ORDER BY code")]

    def list_holdings(self, holdings_date: str, etfs: list[str]) -> list[dict]:
        params: list[str] = [holdings_date]
        where = "holdings_date = ?"
        if etfs:
            placeholders = ",".join("?" for _ in etfs)
            where += f" AND etf_code IN ({placeholders})"
            params.extend(etfs)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT h.*, e.name AS etf_name
                FROM etf_holdings h
                JOIN etfs e ON e.code = h.etf_code
                WHERE {where}
                ORDER BY h.etf_code, h.weight DESC, h.stock_code
                """,
                params,
            )
            return [dict(row) for row in rows]

    def list_holding_dates(self) -> list[str]:
        with self.connect() as conn:
            return [
                row["holdings_date"]
                for row in conn.execute(
                    "SELECT DISTINCT holdings_date FROM etf_holdings ORDER BY holdings_date"
                )
            ]

    def latest_holding_date(self) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(holdings_date) AS date FROM etf_holdings").fetchone()
            return row["date"] if row and row["date"] else None

    def list_price_bars(self, stock_code: str, limit: int | None = None) -> list[dict]:
        params: list[object] = [stock_code]
        sql = """
            SELECT stock_code, date, open, high, low, close, volume, source
            FROM price_bars
            WHERE stock_code = ?
            ORDER BY date
        """
        if limit is not None:
            sql = f"""
                SELECT *
                FROM ({sql})
                ORDER BY date DESC
                LIMIT ?
            """
            params.append(limit)
            outer = True
        else:
            outer = False
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute(sql, params)]
        if outer:
            rows.sort(key=lambda row: row["date"])
        return rows

    def compute_holdings_diff(self, date: str, prev_date: str, etfs: list[str]) -> list[dict]:
        """Compare two holdings snapshots and return trade-like change records."""
        curr_rows = self.list_holdings(holdings_date=date, etfs=etfs)
        prev_rows = self.list_holdings(holdings_date=prev_date, etfs=etfs)

        all_codes = list({r["stock_code"] for r in curr_rows + prev_rows})
        price_map: dict[str, float] = {}
        if all_codes:
            with self.connect() as conn:
                placeholders = ",".join("?" for _ in all_codes)
                rows = conn.execute(
                    f"SELECT code, close FROM daily_quotes WHERE quote_date = ? AND code IN ({placeholders})",
                    [date, *all_codes],
                )
                price_map = {row["code"]: float(row["close"] or 0) for row in rows}

        curr_map = {(r["etf_code"], r["stock_code"]): r for r in curr_rows}
        prev_map = {(r["etf_code"], r["stock_code"]): r for r in prev_rows}
        changes: list[dict] = []

        for (etf_code, stock_code), curr in curr_map.items():
            prev = prev_map.get((etf_code, stock_code))
            price = price_map.get(stock_code, 0.0)
            if prev is None:
                action, delta = "new", curr["lots"]
            elif curr["lots"] > prev["lots"]:
                action, delta = "add", curr["lots"] - prev["lots"]
            elif curr["lots"] < prev["lots"]:
                action, delta = "reduce", prev["lots"] - curr["lots"]
            else:
                continue
            shares = delta * 1000
            changes.append({
                "date": date,
                "etf_code": etf_code,
                "etf_name": curr["etf_name"],
                "stock_code": stock_code,
                "stock_name": curr["stock_name"],
                "sector": "",
                "action": action,
                "shares": shares,
                "price": price,
                "value": price * shares,
            })

        for (etf_code, stock_code), prev in prev_map.items():
            if (etf_code, stock_code) not in curr_map:
                price = price_map.get(stock_code, 0.0)
                shares = prev["lots"] * 1000
                changes.append({
                    "date": date,
                    "etf_code": etf_code,
                    "etf_name": prev["etf_name"],
                    "stock_code": stock_code,
                    "stock_name": prev["stock_name"],
                    "sector": "",
                    "action": "exit",
                    "shares": shares,
                    "price": price,
                    "value": price * shares,
                })

        return changes

    def list_trades_range(self, stock_code: str, from_date: str, to_date: str, etfs: list[str]) -> list[dict]:
        params: list[object] = [stock_code, from_date, to_date]
        where = "t.stock_code = ? AND t.date >= ? AND t.date <= ?"
        if etfs:
            placeholders = ",".join("?" for _ in etfs)
            where += f" AND t.etf_code IN ({placeholders})"
            params.extend(etfs)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT t.*, e.name AS etf_name, s.name AS stock_name, s.sector
                FROM trades t
                JOIN etfs e ON e.code = t.etf_code
                JOIN stocks s ON s.code = t.stock_code
                WHERE {where}
                ORDER BY t.date, t.etf_code
                """,
                params,
            )
            return [dict(row) for row in rows]

    def list_stock_changes_range(self, stock_code: str, from_date: str, to_date: str, etfs: list[str]) -> list[dict]:
        dates = [d for d in self.list_holding_dates() if from_date <= d <= to_date]
        if len(dates) < 2:
            return []
        out: list[dict] = []
        for idx in range(1, len(dates)):
            current = dates[idx]
            prev = dates[idx - 1]
            rows = self.compute_holdings_diff(date=current, prev_date=prev, etfs=etfs)
            for row in rows:
                if row["stock_code"] == stock_code:
                    out.append(row)
        out.sort(key=lambda row: (row["date"], row["etf_code"], row["action"]))
        return out

    def list_trades(self, date: str, etfs: list[str]) -> list[dict]:
        params: list[str] = [date]
        where = "date = ?"
        if etfs:
            placeholders = ",".join("?" for _ in etfs)
            where += f" AND etf_code IN ({placeholders})"
            params.extend(etfs)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT t.*, e.name AS etf_name, s.name AS stock_name, s.sector
                FROM trades t
                JOIN etfs e ON e.code = t.etf_code
                JOIN stocks s ON s.code = t.stock_code
                WHERE {where}
                ORDER BY ABS(t.value) DESC
                """,
                params,
            )
            return [dict(row) for row in rows]
