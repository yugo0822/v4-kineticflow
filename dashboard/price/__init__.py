"""External price provider package.

Quick start:
    from dashboard.price import create_price_provider

    provider = create_price_provider()          # uses PRICE_PROVIDER env var
    price    = provider.get_price("ETHUSDT")    # → 2481.35

Environment variables:
    PRICE_PROVIDER   binance (default) | chainlink | mock
    PRICE_SYMBOL     ETHUSDT (default) — Binance symbol or ignored by Chainlink/mock

Note: entrypoint.sh automatically sets PRICE_PROVIDER=mock when PRICE_SOURCE=mock,
so you normally don't need to set PRICE_PROVIDER manually.
"""
import os

from dashboard.price.base import ExternalPriceProvider


def create_price_provider(provider_name: str | None = None) -> ExternalPriceProvider:
    """Instantiate the configured ExternalPriceProvider.

    Args:
        provider_name: Override the PRICE_PROVIDER env var.
                       Accepted values: "binance", "chainlink", "mock".

    Returns:
        Ready-to-use ExternalPriceProvider instance.
    """
    name = provider_name or os.getenv("PRICE_PROVIDER", "binance")

    if name == "binance":
        from dashboard.price.sources.binance import BinancePriceProvider
        return BinancePriceProvider()

    if name == "chainlink":
        from web3 import Web3
        from dashboard.chain.client import get_rpc_url
        from dashboard.price.sources.chainlink_feed import ChainlinkFeedProvider

        feed_address = os.getenv("CHAINLINK_FEED_ADDRESS")
        w3 = Web3(Web3.HTTPProvider(get_rpc_url()))
        return ChainlinkFeedProvider(w3, feed_address or None)

    if name == "mock":
        from web3 import Web3
        from dashboard.chain.client import get_rpc_url
        from dashboard.price.sources.mock_oracle import MockOraclePriceProvider

        w3 = Web3(Web3.HTTPProvider(get_rpc_url()))
        return MockOraclePriceProvider(w3)

    raise ValueError(
        f"Unknown PRICE_PROVIDER='{name}'. Valid values: binance, chainlink, mock"
    )
