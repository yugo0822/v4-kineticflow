"""
Price Simulator: fluctuates the price of MockV3Aggregator.
For testnet / local simulation only. Not used on mainnet.

Usage:
    python -m dashboard.simulation.price_simulator [--scenario volatile] [--interval 3]

Environment Variables:
    BASE_SEPOLIA_RPC_URL / RPC_URL / ANVIL_RPC_URL: RPC endpoint
    PRIVATE_KEY: Private key for price updates (default: Anvil account 0)
    PRICE_SIMULATOR_INTERVAL: Update interval in seconds (default: 3)

Scenarios (ordered by intensity):
    demo        ±10% sine wave + small noise. Best for stable MPPI demos.
    random_walk ±0.3%/step Gaussian walk. Steady baseline.
    volatile    ±0.5-1.2%/step with occasional jumps ≤±2%. Default demo.
    crash       Gradual downtrend with noise.
    pump        Gradual uptrend with noise.
    extreme     ±1.5-2.5%/step, large jumps. Stress-test only.

Volatility caps (max % move per step at 3 s interval):
    demo        ≈ 0.3%   (approx ±10% range over full run)
    random_walk ≈ 0.3%
    volatile    ≈ 1.2%   (MPPI can track up to ~6% / step = 600 ticks)
    extreme     ≈ 2.5%
"""

import os
import time
import random
import math

from web3 import Web3
from dotenv import load_dotenv

from dashboard.chain.client import get_rpc_url
from dashboard.chain.abis import ORACLE_ABI
from dashboard.config import CONTRACTS

load_dotenv()


class PriceSimulator:
    def __init__(self):
        self.rpc_url = get_rpc_url()
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))

        if not self.w3.is_connected():
            raise ConnectionError(f"Failed to connect to RPC at {self.rpc_url}")

        default_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        env_key = os.getenv("PRIVATE_KEY", "")
        if env_key and env_key.startswith("0x") and len(env_key) == 66:
            self.private_key = env_key
        else:
            self.private_key = default_key

        try:
            self.account = self.w3.eth.account.from_key(self.private_key)
        except Exception as e:
            print(f"❌ Price Simulator: Error parsing private key: {e}", flush=True)
            self.private_key = default_key
            self.account = self.w3.eth.account.from_key(self.private_key)

        oracle_address = CONTRACTS.get("oracle")
        if not oracle_address:
            raise ValueError("Oracle address not found in CONTRACTS.")

        self.oracle = self.w3.eth.contract(
            address=self.w3.to_checksum_address(oracle_address),
            abi=ORACLE_ABI,
        )

        self.decimals = self.oracle.functions.decimals().call()

        try:
            owner = self.oracle.functions.owner().call()
            if owner.lower() != self.account.address.lower():
                print("⚠️ Price Simulator: Oracle owner mismatch. Updates may fail.", flush=True)
        except Exception as e:
            print(f"❌ Price Simulator: Error checking owner: {e}", flush=True)

        # GARCH + Jump-Diffusion parameters
        self.base_vol = 0.003          # 0.3 %/step base (was 0.5%)
        self.current_vol = self.base_vol
        self.vol_persistence = 0.94    # faster mean-reversion (was 0.98)
        self.last_return = 0.0
        self.momentum = 0.0

        self.jump_intensity = 0.005    # unused directly; per-scenario below
        self.jump_size_mean = 0.0
        self.jump_size_std = 0.01      # ±1% jump std (was 2%)

    def get_current_price(self) -> float:
        _, answer, _, _, _ = self.oracle.functions.latestRoundData().call()
        return float(answer) / (10**self.decimals)

    def update_price(self, new_price: float) -> bool:
        """Update oracle price (with retry on nonce errors)."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                scaled_price = int(new_price * (10**self.decimals))
                nonce = self.w3.eth.get_transaction_count(self.account.address, "pending")

                balance = self.w3.eth.get_balance(self.account.address)
                gas_price = self.w3.eth.gas_price
                estimated_gas_cost = 300000 * gas_price

                if balance < estimated_gas_cost:
                    print(
                        f"⚠️ Insufficient balance: {balance/1e18:.6f} ETH < "
                        f"{estimated_gas_cost/1e18:.6f} ETH (gas cost)",
                        flush=True,
                    )
                    return False

                tx = self.oracle.functions.updateAnswer(scaled_price).build_transaction({
                    "from": self.account.address,
                    "nonce": nonce,
                    "gas": 300000,
                    "gasPrice": gas_price,
                })
                signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
                return receipt.status == 1

            except Exception as e:
                error_str = str(e).lower()

                if "insufficient funds" in error_str:
                    try:
                        balance = self.w3.eth.get_balance(self.account.address)
                        balance_eth = balance / 1e18
                        gas_price = self.w3.eth.gas_price
                        estimated_cost = (300000 * gas_price) / 1e18

                        print(
                            f"⚠️ Insufficient funds. Balance: {balance_eth:.6f} ETH, "
                            f"Estimated cost: {estimated_cost:.6f} ETH",
                            flush=True,
                        )

                        if balance_eth < 0.01:
                            print(
                                f"⚠️ CRITICAL: Low ETH balance ({balance_eth:.6f} ETH). "
                                "Price updates paused.",
                                flush=True,
                            )
                            time.sleep(10)
                            return False
                        elif estimated_cost > balance_eth:
                            return False
                    except Exception as balance_error:
                        print(f"⚠️ Error checking balance: {balance_error}", flush=True)

                if "insufficient funds" not in error_str:
                    import traceback
                    print(f"DEBUG: Price update failed at attempt {attempt + 1}", flush=True)
                    print(f"ERROR_LOG: {traceback.format_exc()}", flush=True)

                if "nonce" in error_str or "replacement" in error_str:
                    time.sleep(0.5)
                    continue

                if attempt == max_retries - 1:
                    return False
                time.sleep(0.5)

        return False

    def _generate_market_return(self, scenario: str, step: int, base_price: float, current_price: float) -> float:
        """GARCH + Jump-Diffusion model for next-step return.

        Volatility caps per scenario (% per 3-second step):
            demo        0.3%   extreme → 0.3%
            random_walk 0.3%
            volatile    0.3% → 1.2%  (MPPI max range ≈ 6%)
            crash/pump  0.3% → 0.75%
            extreme     0.3% → 2.5%
        """
        # ── GARCH volatility clustering ───────────────────────────────────────
        shock = abs(self.last_return)
        self.current_vol = (
            self.vol_persistence * self.current_vol
            + (1 - self.vol_persistence) * self.base_vol
            + 0.05 * shock          # shock sensitivity halved (was 0.1)
        )

        # Per-scenario volatility ceiling
        vol_cap = {
            "demo":        self.base_vol * 1.0,   # 0.3%  — never exceeds cap
            "random_walk": self.base_vol * 1.0,   # 0.3%
            "volatile":    self.base_vol * 4.0,   # 1.2%  (was 10x = 5%)
            "crash":       self.base_vol * 2.5,   # 0.75%
            "pump":        self.base_vol * 2.5,   # 0.75%
            "extreme":     self.base_vol * 8.0,   # 2.4%  (was 2.5x × 10x = 12.5%)
        }.get(scenario, self.base_vol * 4.0)

        self.current_vol = max(self.base_vol * 0.5, min(vol_cap, self.current_vol))

        # ── Gentle mean-reversion: pull toward base_price if drifted far ─────
        drift = current_price / base_price - 1.0
        # Reversion kicks in beyond ±15%; full cap at ±30%
        reversion_strength = max(0.0, (abs(drift) - 0.15) / 0.15)
        reversion = -drift * reversion_strength * 0.03

        change = 0.0

        if scenario == "demo":
            # Smooth sine wave ±10% + tiny noise — ideal for dashboard demos
            target = base_price * (1 + 0.10 * math.sin(2 * math.pi * step / 120))
            change = (target / current_price - 1) * 0.15 + random.gauss(0, self.base_vol * 0.5)

        elif scenario == "volatile":
            # Moderate GARCH walk + small jumps (≤±2%)
            change = random.gauss(0, self.current_vol)
            if random.random() < 0.03:                       # 3% jump chance (was 5%)
                jump = random.gauss(0, self.base_vol * 3)    # ±~0.9% std (was base*5)
                change += jump
                if abs(jump) > 0.015:
                    print(f"   >>> MARKET JUMP: {jump:+.2%}", flush=True)

        elif scenario == "extreme":
            # Stress-test: large GARCH + frequent jumps
            change = random.gauss(0, self.current_vol)
            if random.random() < 0.10:                       # 10% jump chance (was 15%)
                jump = random.gauss(0, self.base_vol * 6)    # ±~1.8% std (was base*5 × 2.5)
                change += jump
                if abs(jump) > 0.02:
                    print(f"   >>> MARKET JUMP: {jump:+.2%}", flush=True)

        elif scenario == "crash":
            change = -0.003 + random.gauss(0, self.current_vol)

        elif scenario == "pump":
            change = +0.003 + random.gauss(0, self.current_vol)

        else:  # random_walk
            change = random.gauss(0, self.base_vol)

        # Apply mean-reversion + momentum
        change = change + reversion + (self.last_return * self.momentum)
        self.last_return = change
        return change

    def run_scenario(self, scenario: str = "volatile", base_price: float = 2500.0, interval: float = 3.0):
        """Execute price scenario loop.

        Scenarios: demo, volatile, crash, pump, random_walk, extreme
        """
        print(f"Price Simulator: Started | Scenario: {scenario} | Interval: {interval}s", flush=True)
        current_price = base_price
        step = 0

        while True:
            try:
                ret = self._generate_market_return(scenario, step, base_price, current_price)
                current_price *= 1 + ret
                # Hard floor at 50% of base (was 10%, prevents extreme crashes)
                current_price = max(current_price, base_price * 0.5)

                success = self.update_price(current_price)

                if not success:
                    print(f"[{step:04d}] ❌ Failed to update price: ${current_price:,.2f}", flush=True)
                else:
                    diff = ((current_price / base_price) - 1) * 100
                    if abs(diff) > 2.0 or step % 20 == 0:
                        print(
                            f"[{step:04d}] Price: ${current_price:,.2f} ({diff:+.2f}%) | "
                            f"σ: {self.current_vol:.2%}",
                            flush=True,
                        )

                step += 1
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\nSimulator stopped.")
                break
            except Exception as e:
                print(f"Loop error: {e}", flush=True)
                time.sleep(interval)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Price Simulator for MockV3Aggregator")
    parser.add_argument(
        "--scenario",
        type=str,
        default="volatile",
        choices=["demo", "volatile", "crash", "pump", "random_walk", "extreme"],
    )
    parser.add_argument("--base-price", type=float, default=2500.0)
    parser.add_argument("--interval", type=float, default=3.0)
    args = parser.parse_args()

    simulator = PriceSimulator()
    simulator.run_scenario(
        scenario=args.scenario,
        base_price=args.base_price,
        interval=args.interval,
    )
