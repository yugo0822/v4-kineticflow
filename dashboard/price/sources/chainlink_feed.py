"""On-chain Chainlink AggregatorV3 price provider.

Reads directly from a deployed Chainlink price feed contract.
No gas required (read-only call). Intended for mainnet production use.

Official ETH/USD feed addresses:
    Base mainnet  (8453):  0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70
    Base Sepolia  (84532): 0x4aDC67696bA383F43DD60A9e78F2C97Fbbfc7cb1
    Ethereum main (1):     0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419

Docs: https://docs.chain.link/data-feeds/price-feeds/addresses
"""
from web3 import Web3

from dashboard.price.base import ExternalPriceProvider

# Chain ID → ETH/USD Chainlink feed address
_FEED_ADDRESSES: dict[int, str] = {
    1:     "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419",  # Ethereum mainnet
    8453:  "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70",  # Base mainnet
    84532: "0x4aDC67696bA383F43DD60A9e78F2C97Fbbfc7cb1",  # Base Sepolia
}

# Minimal AggregatorV3Interface ABI
_AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId",         "type": "uint80"},
            {"name": "answer",          "type": "int256"},
            {"name": "startedAt",       "type": "uint256"},
            {"name": "updatedAt",       "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class ChainlinkFeedProvider(ExternalPriceProvider):
    """Read price from a Chainlink AggregatorV3 contract on-chain.

    The *symbol* argument is ignored — the feed address determines the pair.

    Args:
        w3:           Connected Web3 instance pointing at the target chain.
        feed_address: Checksum address of the AggregatorV3 contract.
                      When None, the address is resolved automatically from
                      the chain ID using *_FEED_ADDRESSES*.

    Example (Base mainnet):
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        provider = ChainlinkFeedProvider(w3)
        price = provider.get_price()  # → 2481.35
    """

    def __init__(self, w3: Web3, feed_address: str | None = None):
        self._w3 = w3

        if feed_address is None:
            chain_id = w3.eth.chain_id
            feed_address = _FEED_ADDRESSES.get(chain_id)
            if feed_address is None:
                raise ValueError(
                    f"No Chainlink ETH/USD feed address registered for chain {chain_id}. "
                    "Pass feed_address explicitly or add the chain to _FEED_ADDRESSES."
                )

        self._feed = w3.eth.contract(
            address=w3.to_checksum_address(feed_address),
            abi=_AGGREGATOR_ABI,
        )
        self._decimals: int = self._feed.functions.decimals().call()

    @property
    def name(self) -> str:
        return "Chainlink on-chain"

    def get_price(self, symbol: str = "ETHUSDT") -> float:
        """Return latest answer from the Chainlink feed.

        *symbol* is accepted for interface compatibility but has no effect —
        the feed address determines which asset pair is returned.

        Raises:
            Exception: on RPC failure.
        """
        _, answer, _, _, _ = self._feed.functions.latestRoundData().call()
        return float(answer) / (10 ** self._decimals)
