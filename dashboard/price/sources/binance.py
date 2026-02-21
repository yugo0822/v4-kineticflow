"""Binance REST API price provider.

Free, no authentication required.
Rate limit: 6 000 req/min (weight-based) — far above our ≤6 req/min needs.

Docs: https://binance-docs.github.io/apidocs/spot/en/#symbol-price-ticker
"""
import requests

from dashboard.price.base import ExternalPriceProvider

_REST_URL = "https://api.binance.com/api/v3/ticker/price"


class BinancePriceProvider(ExternalPriceProvider):
    """Fetch latest price via Binance REST ticker endpoint.

    Example:
        provider = BinancePriceProvider()
        price = provider.get_price("ETHUSDT")  # → 2481.35
    """

    def __init__(self, timeout: float = 5.0):
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "Binance REST"

    def get_price(self, symbol: str = "ETHUSDT") -> float:
        """Return latest price for *symbol* from Binance.

        Raises:
            requests.HTTPError: on non-2xx response.
            requests.Timeout:   if the request exceeds *timeout* seconds.
            KeyError:           if the response JSON is malformed.
        """
        resp = requests.get(
            _REST_URL,
            params={"symbol": symbol},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
