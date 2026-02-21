"""MockV3Aggregator price provider.

Reads price from the MockV3Aggregator contract deployed on local Anvil or
testnet.  Used only when PRICE_SOURCE=mock (simulation / local testing).

price_simulator.py writes to the contract; this provider reads from it.
"""
from web3 import Web3

from dashboard.price.base import ExternalPriceProvider
from dashboard.chain.abis import ORACLE_ABI
from dashboard.config import CONTRACTS


class MockOraclePriceProvider(ExternalPriceProvider):
    """Read price from the on-chain MockV3Aggregator.

    Args:
        w3: Connected Web3 instance (Anvil or testnet RPC).

    Raises:
        ValueError: If CONTRACTS['oracle'] is not set.
    """

    def __init__(self, w3: Web3):
        oracle_address = CONTRACTS.get("oracle")
        if not oracle_address:
            raise ValueError(
                "CONTRACTS['oracle'] is not set. "
                "Deploy the MockV3Aggregator first or switch to PRICE_SOURCE=real."
            )
        self._oracle = w3.eth.contract(
            address=w3.to_checksum_address(oracle_address),
            abi=ORACLE_ABI,
        )
        self._decimals: int = self._oracle.functions.decimals().call()

    @property
    def name(self) -> str:
        return "MockV3Aggregator"

    def get_price(self, symbol: str = "ETHUSDT") -> float:
        """Return latest answer from MockV3Aggregator.

        *symbol* is ignored — the deployed oracle determines the pair.
        """
        _, answer, _, _, _ = self._oracle.functions.latestRoundData().call()
        return float(answer) / (10 ** self._decimals)
