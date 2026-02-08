"""
ENS (Ethereum Name Service) utilities for resolving addresses to .eth names.
Uses Ethereum mainnet for resolution (ENS registry lives on mainnet).
Only depends on web3>=6; no separate ens package.
Optional: set ETH_MAINNET_RPC_URL in .env to enable ENS display in the dashboard.
"""

import os
from typing import Optional

# Ethereum mainnet: ENS Registry
ENS_REGISTRY_ADDRESS = "0x00000000000C2E074eC69A0dFb2997BA6C7d2e1e"

_w3_mainnet = None


def _namehash(name: str) -> bytes:
    """Compute ENS namehash (e.g. 'addr.reverse' -> 32 bytes)."""
    if not name or name.strip() == "":
        return b"\x00" * 32
    from web3 import Web3
    labels = name.split(".")
    # namehash(n) = keccak256(namehash(n[1:]) + keccak256(n[0]))
    parent = _namehash(".".join(labels[1:])) if len(labels) > 1 else b"\x00" * 32
    label_hash = Web3.keccak(primitive=labels[0].encode("utf-8"))
    combined = parent + (bytes(label_hash) if hasattr(label_hash, "__bytes__") else label_hash)
    out = Web3.keccak(primitive=combined)
    return bytes(out) if out is not None else b"\x00" * 32


def _get_w3_mainnet():
    """Get Web3 instance for Ethereum mainnet (ENS resolution only)."""
    global _w3_mainnet
    if _w3_mainnet is not None:
        return _w3_mainnet
    rpc = os.getenv("ETH_MAINNET_RPC_URL") or os.getenv("ETHEREUM_MAINNET_RPC_URL")
    if not rpc:
        return None
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc))
        if not w3.is_connected():
            return None
        _w3_mainnet = w3
        return _w3_mainnet
    except Exception:
        return None


def resolve_address_to_ens(address: str) -> Optional[str]:
    """
    Reverse-resolve an Ethereum address to its primary ENS name (e.g. vitalik.eth).
    Returns None if ENS is not configured, resolution fails, or the address has no primary name.
    Uses web3 only (no ens package); compatible with web3>=6.
    """
    if not address or not isinstance(address, str) or not address.startswith("0x"):
        return None
    w3 = _get_w3_mainnet()
    if w3 is None:
        return None
    try:
        addr_hex = address.lower().replace("0x", "").strip()
        if len(addr_hex) != 40:
            return None
        # Reverse resolution: "addr.reverse" subdomain is <reversed hex>.addr.reverse
        reversed_hex = addr_hex[::-1]
        reverse_name = f"{reversed_hex}.addr.reverse"
        node = _namehash(reverse_name)

        registry_abi = [
            {"inputs": [{"name": "node", "type": "bytes32"}], "name": "resolver", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"}
        ]
        registry = w3.eth.contract(address=w3.to_checksum_address(ENS_REGISTRY_ADDRESS), abi=registry_abi)
        resolver_address = registry.functions.resolver(node).call()
        if not resolver_address or int(resolver_address, 16) == 0:
            return None

        # Legacy resolver: name(bytes32 node) returns (string)
        resolver_abi = [
            {"inputs": [{"name": "node", "type": "bytes32"}], "name": "name", "outputs": [{"name": "", "type": "string"}], "stateMutability": "view", "type": "function"}
        ]
        resolver = w3.eth.contract(address=w3.to_checksum_address(resolver_address), abi=resolver_abi)
        name = resolver.functions.name(node).call()
        if name and isinstance(name, str) and len(name) > 0 and ".eth" in name:
            return name
        return None
    except Exception:
        return None


def format_address_with_ens(address: str, max_chars: int = 10) -> str:
    """
    Format an address for display: "name.eth (0x1234...abcd)" or "0x1234...abcd".
    """
    ens_name = resolve_address_to_ens(address)
    short = f"{address[: max_chars + 2]}...{address[-4:]}" if len(address) > (max_chars + 6) else address
    if ens_name:
        return f"{ens_name} ({short})"
    return short
