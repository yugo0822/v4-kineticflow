"""
Shared Uniswap v4 pool utilities.

All functions are stateless and can be used by any module.
"""

from typing import Optional, Tuple
from web3 import Web3
from eth_abi import encode


def compute_pool_id(
    token0: str,
    token1: str,
    fee: int,
    tick_spacing: int,
    hooks: str,
) -> bytes:
    """Compute Uniswap v4 pool ID = keccak256(abi.encode(PoolKey)).
    Token order is normalized (lower address first) automatically.
    """
    token0 = Web3.to_checksum_address(token0)
    token1 = Web3.to_checksum_address(token1)
    hooks = Web3.to_checksum_address(hooks)
    if token0.lower() > token1.lower():
        token0, token1 = token1, token0
    encoded = encode(
        ["address", "address", "uint24", "int24", "address"],
        [token0, token1, fee, tick_spacing, hooks],
    )
    return Web3.keccak(encoded)


def fetch_slot0(pool_manager, pool_id: bytes) -> Optional[dict]:
    """Read pool state from PoolManager via extsload (with getSlot0 fallback).

    Returns a dict with keys: sqrtPriceX96, tick, price, protocolFee, lpFee.
    Returns None if the pool is uninitialised or an error occurs.
    """
    try:
        pools_slot = b"\x00" * 31 + b"\x06"
        slot = Web3.keccak(pool_id + pools_slot)
        value = pool_manager.functions.extsload(slot).call()
        data = int.from_bytes(value, byteorder="big")

        if data == 0:
            return None

        sqrt_price_x96 = data & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
        if sqrt_price_x96 == 0 or sqrt_price_x96 > (1 << 160) - 1:
            return None

        tick_raw = (data >> 160) & 0xFFFFFF
        tick = tick_raw - (1 << 24) if tick_raw & (1 << 23) else tick_raw

        if not (-887272 <= tick <= 887272):
            return None

        price = (sqrt_price_x96 / (2**96)) ** 2
        if price <= 0 or price > 1e10:
            price = 1.0001**tick
            if price <= 0 or price > 1e10:
                return None

        protocol_fee = (data >> 184) & 0xFFFFFF
        lp_fee = (data >> 208) & 0xFFFFFF

        return {
            "sqrtPriceX96": sqrt_price_x96,
            "tick": tick,
            "price": price,
            "protocolFee": protocol_fee,
            "lpFee": lp_fee,
        }
    except Exception:
        pass

    # Fallback: getSlot0
    try:
        result = pool_manager.functions.getSlot0(pool_id).call()
        sqrt_price_x96 = result[0]
        tick = result[1]
        if sqrt_price_x96 == 0:
            return None
        price = (sqrt_price_x96 / (2**96)) ** 2
        return {
            "sqrtPriceX96": sqrt_price_x96,
            "tick": tick,
            "price": price,
            "protocolFee": result[2],
            "lpFee": result[3],
        }
    except Exception:
        return None


def fetch_liquidity(pool_manager, pool_id: bytes) -> Optional[int]:
    """Fetch total active liquidity from PoolManager storage."""
    try:
        pools_slot = b"\x00" * 31 + b"\x06"
        state_slot = Web3.keccak(pool_id + pools_slot)
        liquidity_slot = (int.from_bytes(state_slot, "big") + 3).to_bytes(32, "big")
        value = pool_manager.functions.extsload(liquidity_slot).call()
        return int.from_bytes(value, "big") & ((1 << 128) - 1)
    except Exception:
        return None


def decode_position_ticks(position_info_int: int) -> Tuple[int, int]:
    """Decode tickLower and tickUpper from packed PositionInfo uint256.

    Layout: 200 bits poolId | 24 bits tickUpper | 24 bits tickLower | 8 bits hasSubscriber
    """
    tick_lower_raw = (position_info_int >> 8) & 0xFFFFFF
    tick_lower = tick_lower_raw - (1 << 24) if tick_lower_raw & (1 << 23) else tick_lower_raw

    tick_upper_raw = (position_info_int >> 32) & 0xFFFFFF
    tick_upper = tick_upper_raw - (1 << 24) if tick_upper_raw & (1 << 23) else tick_upper_raw

    return tick_lower, tick_upper


def find_active_position(
    posm,
    pool_id: bytes,
    max_lookback: int = 10,
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Search backward from the latest token ID for the most recent active position
    that belongs to `pool_id` and has non-zero liquidity.

    Returns (token_id, tick_lower, tick_upper), or (None, None, None) if not found.
    """
    try:
        next_token_id = posm.functions.nextTokenId().call()
    except Exception:
        return None, None, None

    for i in range(max_lookback):
        token_id = next_token_id - 1 - i
        if token_id < 1:
            break
        try:
            pool_key, position_info = posm.functions.getPoolAndPositionInfo(token_id).call()
            liquidity = posm.functions.getPositionLiquidity(token_id).call()
            if liquidity == 0:
                continue

            pool_key_tuple = (pool_key[0], pool_key[1], pool_key[2], pool_key[3], pool_key[4])
            encoded = encode(
                ["address", "address", "uint24", "int24", "address"],
                pool_key_tuple,
            )
            if Web3.keccak(encoded) != pool_id:
                continue

            tick_lower, tick_upper = decode_position_ticks(int(position_info))
            return token_id, tick_lower, tick_upper
        except Exception:
            continue

    return None, None, None
