"""Abstract interface for external price providers."""
from abc import ABC, abstractmethod


class ExternalPriceProvider(ABC):
    """Unified interface for any external price source.

    Implementations:
        BinancePriceProvider   – Binance REST API (default, free, no auth)
        ChainlinkFeedProvider  – On-chain Chainlink AggregatorV3 (mainnet/testnet)
    """

    @abstractmethod
    def get_price(self, symbol: str = "ETHUSDT") -> float:
        """Return the latest price for *symbol*.

        Args:
            symbol: Trading pair in the format understood by the provider
                    (e.g. "ETHUSDT" for Binance, ignored for Chainlink).

        Returns:
            Price as a float (USD value of 1 unit of the base asset).

        Raises:
            Exception: propagated on network / RPC failure so callers can
                       implement their own retry / fallback.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name used in log messages."""
        ...
