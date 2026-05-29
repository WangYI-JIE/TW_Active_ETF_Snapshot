from __future__ import annotations

import requests

from app.pipeline.providers.moneydj import MoneyDjHoldingsProvider
from app.pipeline.providers.twse import TwseActiveEtfProvider


class ActiveEtfProvider:
    """Production provider composed from TWSE ETF universe and MoneyDJ holdings."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.twse = TwseActiveEtfProvider(self.session)
        self.holdings = MoneyDjHoldingsProvider(session=self.session)

    def fetch_etfs(self) -> list[dict]:
        return self.twse.fetch_etfs()

    def fetch_stocks(self) -> list[dict]:
        return []

    def fetch_trades(self, date: str, etfs: list[str]) -> list[dict]:
        return []

    def fetch_holdings(self, date: str, etfs: list[dict]) -> tuple[list[dict], list[dict]]:
        return self.holdings.fetch_holdings(date, etfs)
