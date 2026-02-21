import os
from web3 import Web3


def get_rpc_url() -> str:
    return (
        os.getenv("BASE_SEPOLIA_RPC_URL")
        or os.getenv("RPC_URL")
        or os.getenv("ANVIL_RPC_URL")
        or "http://127.0.0.1:8545"
    )


def get_web3() -> Web3:
    return Web3(Web3.HTTPProvider(get_rpc_url()))
