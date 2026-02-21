"""
SwapBot: arbitrage + noise trader for testnet / local simulation only.
Not used on mainnet.
"""

import os
import time
import random
import sqlite3
import threading

from web3 import Web3
from dotenv import load_dotenv

from dashboard.chain.client import get_rpc_url
from dashboard.chain.abis import ERC20_ABI, PERMIT2_ABI, POOL_MANAGER_ABI, SWAP_ROUTER_ABI
from dashboard.chain.pool import compute_pool_id, fetch_slot0, fetch_liquidity
from dashboard.data_store import store
from dashboard.config import CONTRACTS

load_dotenv()


class SwapBot:
    def __init__(self):
        self.rpc_url = get_rpc_url()
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))

        # Use a dedicated key for arbitrage to allow "infinite balance" minting locally
        # without accidentally affecting MPPI position sizing.
        self.private_key = os.getenv(
            "BOT_PRIVATE_KEY",
            "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
        )
        self.account = self.w3.eth.account.from_key(self.private_key)

        # Simple portfolio tracking (token1-valued)
        self._initial_portfolio_value_token1 = None
        self._last_portfolio_value_token1 = None

        self.pool_manager_address = Web3.to_checksum_address(CONTRACTS["pool_manager"])
        self.permit2_address = Web3.to_checksum_address(
            CONTRACTS.get("permit2", "0x000000000022D473030F116dDEE9F6B43aC78BA3")
        )
        self.swap_router_address = Web3.to_checksum_address(CONTRACTS["swap_router"])
        self.token0_address = Web3.to_checksum_address(CONTRACTS["token0"])
        self.token1_address = Web3.to_checksum_address(CONTRACTS["token1"])
        self.hook_address = Web3.to_checksum_address(CONTRACTS["hook"])

        self.fee = 3000
        self.tick_spacing = 60

        self.pool_manager = self.w3.eth.contract(address=self.pool_manager_address, abi=POOL_MANAGER_ABI)
        self.router = self.w3.eth.contract(address=self.swap_router_address, abi=SWAP_ROUTER_ABI)
        self.permit2 = self.w3.eth.contract(address=self.permit2_address, abi=PERMIT2_ABI)

        self.approve_tokens()
        self.ensure_infinite_balance()

    # ------------------------------------------------------------------
    # Pool helpers
    # ------------------------------------------------------------------

    def get_pool_id(self) -> bytes:
        return compute_pool_id(
            self.token0_address, self.token1_address,
            self.fee, self.tick_spacing, self.hook_address,
        )

    # ------------------------------------------------------------------
    # Token approvals
    # ------------------------------------------------------------------

    def approve_tokens(self):
        """Approve tokens for Router and Permit2."""
        max_uint256 = 2**256 - 1
        max_uint160 = 2**160 - 1
        max_uint48 = 2**48 - 1

        spenders = [
            ("SwapRouter", self.swap_router_address),
            ("Permit2", self.permit2_address),
        ]

        for token_addr in [self.token0_address, self.token1_address]:
            token = self.w3.eth.contract(address=token_addr, abi=ERC20_ABI)

            for _, spender_addr in spenders:
                try:
                    current_allowance = token.functions.allowance(self.account.address, spender_addr).call()
                    if current_allowance < max_uint256 // 2:
                        tx = token.functions.approve(spender_addr, max_uint256).build_transaction({
                            "from": self.account.address,
                            "nonce": self.w3.eth.get_transaction_count(self.account.address),
                            "gas": 200000,
                            "gasPrice": self.w3.eth.gas_price,
                        })
                        signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
                        tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                        self.w3.eth.wait_for_transaction_receipt(tx_hash)
                except Exception as e:
                    print(f"Token approval error: {e}", flush=True)

            try:
                p2_allowance = self.permit2.functions.allowance(
                    self.account.address, token_addr, self.swap_router_address
                ).call()
                if p2_allowance[0] < max_uint160 // 2:
                    tx = self.permit2.functions.approve(
                        token_addr, self.swap_router_address, max_uint160, max_uint48
                    ).build_transaction({
                        "from": self.account.address,
                        "nonce": self.w3.eth.get_transaction_count(self.account.address),
                        "gas": 200000,
                        "gasPrice": self.w3.eth.gas_price,
                    })
                    signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
                    tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                    receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
                    if receipt["status"] != 1:
                        print(f"❌ Permit2 Approval failed for {token_addr}", flush=True)
            except Exception as e:
                print(f"Permit2 approval error: {e}", flush=True)

    def ensure_infinite_balance(self):
        """Local Anvil helper: keep arb bot balances topped up via token.mint().
        Only effective for local simulation; silently skipped on real networks.
        """
        enabled = os.getenv("ARB_INFINITE_BALANCE", "1").lower() in ("1", "true", "yes", "on")
        if not enabled:
            return

        target = int(1_000_000_000 * 1e18)
        refill_threshold = int(target * 0.2)

        for token_addr in [self.token0_address, self.token1_address]:
            try:
                token = self.w3.eth.contract(address=token_addr, abi=ERC20_ABI)
                bal = token.functions.balanceOf(self.account.address).call()
                if bal >= refill_threshold:
                    continue
                amount_to_mint = target - bal
                if amount_to_mint <= 0:
                    continue
                tx = token.functions.mint(self.account.address, int(amount_to_mint)).build_transaction({
                    "from": self.account.address,
                    "nonce": self.w3.eth.get_transaction_count(self.account.address),
                    "gas": 300_000,
                    "gasPrice": self.w3.eth.gas_price,
                })
                signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
                if receipt["status"] != 1:
                    print(f"❌ Mint failed for {token_addr}", flush=True)
                else:
                    print(f"🪙 Minted to arb bot: {token_addr} (+{amount_to_mint/1e18:.0f})", flush=True)
            except Exception as e:
                print(
                    f"⚠️ Could not mint {token_addr} (arb infinite balance disabled for this token): {e}",
                    flush=True,
                )

    # ------------------------------------------------------------------
    # Swap execution
    # ------------------------------------------------------------------

    def execute_swap(self, zero_for_one: bool, amount_in: int) -> bool:
        """Execute a swap via SwapRouter."""
        try:
            token_in = self.token0_address if zero_for_one else self.token1_address
            token_contract = self.w3.eth.contract(address=token_in, abi=ERC20_ABI)
            self.ensure_infinite_balance()

            balance = token_contract.functions.balanceOf(self.account.address).call()
            if balance < amount_in:
                clamped = int(balance * 0.95)
                if clamped <= 0:
                    print(f"Insufficient balance for swap. Has {balance}, needs {amount_in}", flush=True)
                    return False
                amount_in = clamped
                if amount_in < int(0.001 * 1e18):
                    print(f"Insufficient balance for swap (too small after clamp). Has {balance}", flush=True)
                    return False

            max_uint256 = 2**256 - 1
            router_allowance = token_contract.functions.allowance(
                self.account.address, self.swap_router_address
            ).call()
            if router_allowance < max_uint256 // 2:
                tx = token_contract.functions.approve(self.swap_router_address, max_uint256).build_transaction({
                    "from": self.account.address,
                    "nonce": self.w3.eth.get_transaction_count(self.account.address),
                    "gas": 200000,
                    "gasPrice": self.w3.eth.gas_price,
                })
                signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
                self.w3.eth.wait_for_transaction_receipt(
                    self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                )

            pool_key = (
                self.token0_address,
                self.token1_address,
                self.fee,
                self.tick_spacing,
                self.hook_address,
            )

            nonce = self.w3.eth.get_transaction_count(self.account.address)
            amount_in_uint256 = int(amount_in) if amount_in <= 2**255 - 1 else amount_in

            tx = self.router.functions.swapExactTokensForTokens(
                amount_in_uint256, 0, zero_for_one, pool_key, b"",
                self.account.address, int(time.time()) + 600,
            ).build_transaction({
                "from": self.account.address,
                "nonce": nonce,
                "gas": 1000000,
                "gasPrice": self.w3.eth.gas_price,
            })

            signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)

            if receipt["status"] == 1:
                try:
                    pool_id = self.get_pool_id()
                    slot0_data = fetch_slot0(self.pool_manager, pool_id)
                    if slot0_data:
                        price_after = slot0_data["price"]
                        tick_after = slot0_data["tick"]
                        sqrt_price_x96 = slot0_data["sqrtPriceX96"]

                        try:
                            bal0 = self.w3.eth.contract(address=self.token0_address, abi=ERC20_ABI).functions.balanceOf(self.account.address).call()
                            bal1 = self.w3.eth.contract(address=self.token1_address, abi=ERC20_ABI).functions.balanceOf(self.account.address).call()
                            v_token1 = (bal1 / 1e18) + (bal0 / 1e18) * float(price_after)
                            if self._initial_portfolio_value_token1 is None:
                                self._initial_portfolio_value_token1 = v_token1
                            self._last_portfolio_value_token1 = v_token1
                            pnl_token1 = v_token1 - self._initial_portfolio_value_token1
                            print(
                                f"📊 Arb PnL: value={v_token1:.4f} token1, PnL={pnl_token1:+.4f} token1",
                                flush=True,
                            )
                        except Exception:
                            v_token1 = None
                            pnl_token1 = None

                        liquidity_after = fetch_liquidity(self.pool_manager, pool_id) or 0

                        print(
                            f"✅ Swap: {'0->1' if zero_for_one else '1->0'} | "
                            f"{amount_in/1e18:.2f} tokens | Price: {price_after:.4f} | "
                            f"Tick: {tick_after} | Liquidity: {liquidity_after/1e18:.2f} | "
                            f"{tx_hash.hex()[:10]}...",
                            flush=True,
                        )

                        try:
                            gas_used = int(receipt.get("gasUsed", 0))
                            gas_price = int(receipt.get("effectiveGasPrice", self.w3.eth.gas_price))
                            cost_eth = (gas_used * gas_price) / 1e18
                            store.append_tx_event(
                                timestamp=time.time(),
                                actor="arb",
                                event_type="swap",
                                tx_hash=tx_hash.hex(),
                                gas_used=gas_used,
                                gas_price_wei=gas_price,
                                cost_eth=cost_eth,
                                pool_price=float(price_after),
                                portfolio_value_token1=v_token1,
                                portfolio_pnl_token1=pnl_token1,
                                meta={
                                    "direction": "0->1" if zero_for_one else "1->0",
                                    "amount_in_e18": int(amount_in),
                                    "tick_after": int(tick_after),
                                },
                            )
                        except Exception:
                            pass
                    else:
                        print(
                            f"✅ Swap: {'0->1' if zero_for_one else '1->0'} | "
                            f"{amount_in/1e18:.2f} tokens | {tx_hash.hex()[:10]}... "
                            "(Error fetching post-swap state)",
                            flush=True,
                        )
                except Exception as e:
                    print(
                        f"✅ Swap: {'0->1' if zero_for_one else '1->0'} | "
                        f"{amount_in/1e18:.2f} tokens | {tx_hash.hex()[:10]}... "
                        f"(Error fetching post-swap state: {e})",
                        flush=True,
                    )
                return True
            else:
                print(
                    f"❌ Swap reverted: {'0->1' if zero_for_one else '1->0'} | Hash: {tx_hash.hex()[:10]}...",
                    flush=True,
                )
                try:
                    pool_key_named = (
                        self.token0_address, self.token1_address,
                        self.fee, self.tick_spacing, self.hook_address,
                    )
                    self.router.functions.swapExactTokensForTokens(
                        amount_in_uint256, 0, zero_for_one, pool_key_named, b"",
                        self.account.address, int(time.time()) + 600,
                    ).call({"from": self.account.address, "gas": 1000000})
                except Exception as call_error:
                    print(f"Revert reason (from call): {call_error}", flush=True)
                return False

        except Exception as e:
            import traceback
            print(f"Swap failed: {type(e).__name__}: {e}", flush=True)
            print(f"Traceback: {traceback.format_exc()}", flush=True)
            return False

    # ------------------------------------------------------------------
    # Trading strategies
    # ------------------------------------------------------------------

    def calculate_optimal_amount(self, pool_price: float, ext_price: float, liquidity: int):
        """Calculate swap amount based on price difference."""
        if liquidity == 0:
            return True, 0

        diff_ratio = abs(pool_price - ext_price) / ext_price
        zero_for_one = pool_price > ext_price

        if zero_for_one:
            base_amount = 1.0 * 1e18
            max_amount = 5.0 * 1e18
        else:
            base_amount = 1.0 * 1e18 * pool_price
            max_amount = 5.0 * 1e18 * pool_price

        amount = int(base_amount * diff_ratio * 10)
        min_amount = int(0.01 * 1e18)
        amount = max(min_amount, min(amount, int(max_amount)))
        return zero_for_one, amount

    def run_noise_trader(self):
        """Noise trader: randomly buy and sell."""
        while True:
            try:
                time.sleep(random.uniform(5, 15))
                zero_for_one = random.choice([True, False])
                amount = random.uniform(0.1, 1.0) * 1e18
                self.execute_swap(zero_for_one, amount)
            except Exception as e:
                print(f"Noise trader error: {e}", flush=True)
                time.sleep(5)

    def run_arbitrage_bot(self):
        """Arbitrage bot: close price deviation."""
        while True:
            try:
                time.sleep(3)

                with sqlite3.connect(store.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT pool_price, external_price, pool_liquidity, price_lower, price_upper "
                        "FROM price_history ORDER BY timestamp DESC LIMIT 1"
                    )
                    row = cursor.fetchone()

                if not row:
                    continue

                pool_price, ext_price, liquidity_raw, price_lower, price_upper = row
                liquidity = int(float(liquidity_raw) * 1e18) if liquidity_raw is not None else 0

                if liquidity == 0:
                    continue

                if price_lower and price_upper:
                    if pool_price < price_lower or pool_price > price_upper:
                        continue

                diff_ratio = (pool_price - ext_price) / ext_price
                THRESHOLD = 0.005

                if abs(diff_ratio) > THRESHOLD:
                    zero_for_one, optimal_amount = self.calculate_optimal_amount(pool_price, ext_price, liquidity)
                    if optimal_amount > 0:
                        direction = "0->1" if zero_for_one else "1->0"
                        print(
                            f"⚖️ Arb: Target={ext_price:.2f} | {direction} | Amount={optimal_amount/1e18:.4f}",
                            flush=True,
                        )
                        self.execute_swap(zero_for_one, optimal_amount)

            except Exception as e:
                print(f"Arbitrage bot error: {e}", flush=True)
                time.sleep(5)


if __name__ == "__main__":
    bot = SwapBot()

    t1 = threading.Thread(target=bot.run_noise_trader)
    t2 = threading.Thread(target=bot.run_arbitrage_bot)

    t1.start()
    t2.start()

    t1.join()
    t2.join()
