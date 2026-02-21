import os
import time
import random

from web3 import Web3
from dotenv import load_dotenv

from dashboard.chain.client import get_rpc_url
from dashboard.chain.abis import POOL_MANAGER_ABI, POSITION_MANAGER_ABI, ORACLE_ABI
from dashboard.chain.pool import fetch_slot0, fetch_liquidity, find_active_position, compute_pool_id
from dashboard.config import CONTRACTS

load_dotenv()


class MarketMonitor:
    def __init__(self):
        self.rpc_url = get_rpc_url()
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))

        self.pool_manager = self.w3.eth.contract(
            address=self.w3.to_checksum_address(CONTRACTS["pool_manager"]),
            abi=POOL_MANAGER_ABI,
        )

        position_manager_address = CONTRACTS.get("position_manager")
        if position_manager_address:
            self.position_manager = self.w3.eth.contract(
                address=self.w3.to_checksum_address(position_manager_address),
                abi=POSITION_MANAGER_ABI,
            )
        else:
            self.position_manager = None

        self.history = []

    # ------------------------------------------------------------------
    # Price / tick helpers
    # ------------------------------------------------------------------

    def tick_to_price(self, tick: int) -> float:
        return 1.0001**tick

    # ------------------------------------------------------------------
    # On-chain data fetching (delegates to chain.pool helpers)
    # ------------------------------------------------------------------

    def fetch_onchain_price(self, pool_id: bytes) -> dict | None:
        """Fetch pool state from PoolManager. Returns None if uninitialized."""
        return fetch_slot0(self.pool_manager, pool_id)

    def fetch_pool_liquidity(self, pool_id: bytes) -> int | None:
        """Fetch total active liquidity."""
        return fetch_liquidity(self.pool_manager, pool_id)

    def fetch_position_ticks(self, pool_id_bytes: bytes | None = None):
        """Fetch tick range of the most recent active position."""
        if not self.position_manager:
            return None, None
        _, tick_lower, tick_upper = find_active_position(self.position_manager, pool_id_bytes)
        return tick_lower, tick_upper

    def fetch_mock_oracle_price(self) -> float | None:
        """Fetch price from MockV3Aggregator oracle."""
        try:
            oracle_address = CONTRACTS.get("oracle")
            if not oracle_address:
                return None

            oracle = self.w3.eth.contract(
                address=self.w3.to_checksum_address(oracle_address),
                abi=ORACLE_ABI,
            )
            _, answer, _, _, _ = oracle.functions.latestRoundData().call()
            decimals = oracle.functions.decimals().call()
            return float(answer) / (10**decimals)
        except Exception as e:
            print(f"Error fetching mock oracle price: {e}", flush=True)
            return None

    # ------------------------------------------------------------------
    # Main monitoring loop
    # ------------------------------------------------------------------

    def run_monitoring(self, pool_id: bytes, interval: int = 2):
        """Collect data periodically and save to DB."""
        print(f"Monitor: Started for pool {pool_id.hex()[:16]}...", flush=True)
        from dashboard.data_store import store

        last_valid_price = None
        last_valid_tick = None

        while True:
            onchain = self.fetch_onchain_price(pool_id)
            external_price = self.fetch_mock_oracle_price()

            if external_price is None:
                current_pool_price = (
                    onchain["price"] if onchain and onchain.get("price", 0) > 0 else (last_valid_price or 2500)
                )
                external_price = current_pool_price + random.uniform(-10, 10)
                print("Warning: Using fallback dummy price (oracle unavailable)", flush=True)

            if onchain and onchain.get("price", 0) > 0:
                # Resolve pool_id to bytes for comparison
                pool_id_bytes = pool_id if isinstance(pool_id, bytes) else bytes.fromhex(
                    pool_id[2:] if pool_id.startswith("0x") else pool_id
                )

                fetched_tick_lower, fetched_tick_upper = self.fetch_position_ticks(pool_id_bytes)

                if fetched_tick_lower is not None and fetched_tick_upper is not None:
                    tick_lower = fetched_tick_lower
                    tick_upper = fetched_tick_upper
                else:
                    # Fallback: ±2000 ticks around default target tick
                    target_tick = 78240
                    tick_lower = target_tick - 2000
                    tick_upper = target_tick + 2000

                # Clamp price to tick range boundary when price is out of range
                if onchain["tick"] < tick_lower:
                    price_lower = self.tick_to_price(tick_lower)
                    if onchain["price"] < price_lower * 0.99:
                        onchain["price"] = price_lower
                elif onchain["tick"] > tick_upper:
                    price_upper = self.tick_to_price(tick_upper)
                    if onchain["price"] > price_upper * 1.01:
                        onchain["price"] = price_upper

                if last_valid_price is None:
                    last_valid_price = onchain["price"]
                    last_valid_tick = onchain["tick"]

                pool_liquidity = self.fetch_pool_liquidity(pool_id)

                price_lower = self.tick_to_price(tick_lower)
                price_upper = self.tick_to_price(tick_upper)

                diff_absolute = onchain["price"] - external_price
                diff_ratio = diff_absolute / external_price if external_price > 0 else 0

                data = {
                    "timestamp": time.time(),
                    "pool_price": onchain["price"],
                    "external_price": external_price,
                    "tick": onchain["tick"],
                    "tick_lower": tick_lower,
                    "tick_upper": tick_upper,
                    "price_lower": price_lower,
                    "price_upper": price_upper,
                    "pool_liquidity": pool_liquidity if pool_liquidity else 0,
                    "diff": diff_absolute,
                    "diff_ratio": diff_ratio,
                }

                store.append_data(data)

                rebalance_threshold = 0.005
                if abs(diff_ratio) > rebalance_threshold:
                    status = "⚠️ REBALANCE NEEDED"
                elif abs(diff_ratio) > rebalance_threshold * 0.5:
                    status = "⚡ Monitor"
                else:
                    status = "✓ OK"

                in_range = price_lower <= onchain["price"] <= price_upper
                should_log = (
                    abs(diff_ratio) > rebalance_threshold
                    or not in_range
                    or (
                        last_valid_price is not None
                        and abs(onchain["price"] - last_valid_price) / last_valid_price > 0.01
                    )
                )

                if should_log:
                    print(
                        f"[{time.strftime('%H:%M:%S')}] Pool: {data['pool_price']:.4f} | "
                        f"Ext: {data['external_price']:.4f} | Diff: {diff_ratio*100:.2f}% | {status}",
                        flush=True,
                    )
                    if not in_range:
                        actual_price_from_tick = self.tick_to_price(onchain["tick"])
                        print(
                            f"  ⚠️ Out of Range: [{price_lower:.2f}, {price_upper:.2f}] | "
                            f"Tick: {onchain['tick']} (range: {tick_lower}-{tick_upper}) | "
                            f"Actual price from tick: {actual_price_from_tick:.4f}",
                            flush=True,
                        )

                last_valid_price = onchain["price"]
                last_valid_tick = onchain["tick"]
            else:
                if last_valid_price:
                    print(
                        f"Waiting for pool data... (last valid price: {last_valid_price:.4f}, tick: {last_valid_tick})",
                        flush=True,
                    )
                else:
                    print("Waiting for pool data...", flush=True)

            time.sleep(interval)


if __name__ == "__main__":
    import traceback

    print("=" * 60, flush=True)
    print(f"CONTRACTS loaded: {CONTRACTS}", flush=True)
    print("=" * 60, flush=True)

    try:
        if "token0" not in CONTRACTS or "token1" not in CONTRACTS or "hook" not in CONTRACTS:
            print(f"ERROR: Missing required contract addresses in CONTRACTS: {CONTRACTS}", flush=True)
            raise ValueError("Missing contract addresses")

        POOL_ID = compute_pool_id(
            CONTRACTS["token0"],
            CONTRACTS["token1"],
            3000,
            60,
            CONTRACTS["hook"],
        )
        print(f"Computed Pool ID: {POOL_ID.hex()}", flush=True)

        monitor = MarketMonitor()
        monitor.run_monitoring(POOL_ID)

    except KeyboardInterrupt:
        print("Monitoring stopped by user.", flush=True)
    except Exception as e:
        print(f"CRITICAL ERROR in monitor: {type(e).__name__}: {e}", flush=True)
        print(f"Traceback:\n{traceback.format_exc()}", flush=True)
        time.sleep(5)
        raise
