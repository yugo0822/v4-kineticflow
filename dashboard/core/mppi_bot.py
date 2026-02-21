import time
import math
import os
import sqlite3

import torch
import numpy as np
from web3 import Web3
from dotenv import load_dotenv
from eth_abi import encode

from dashboard.chain.client import get_rpc_url
from dashboard.chain.abis import ERC20_ABI, PERMIT2_ABI, POOL_MANAGER_ABI, POSITION_MANAGER_ABI
from dashboard.chain.pool import compute_pool_id, fetch_slot0, decode_position_ticks, find_active_position
from dashboard.data_store import store
from dashboard.config import CONTRACTS
from dashboard.optimizer.controller import S_MPPI
from dashboard.optimizer.config_for_mppi import MPPI_CONFIG
from dashboard.optimizer.cost_function import stage_cost, terminal_cost
from dashboard.optimizer.utils import (
    uniswap_dynamics,
    generate_jump_diffusion_parameter_seq,
    generate_constant_parameter_seq,
)

load_dotenv()


class MPPIBot:
    def __init__(self):
        self.device = torch.device("cpu")
        self.dtype = torch.float32

        self.rpc_url = get_rpc_url()
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))

        # Use a dedicated key for MPPI to avoid sharing balances with arbitrage bot.
        # Fallback to BOT_PRIVATE_KEY for backward compatibility.
        self.private_key = os.getenv(
            "MPPI_PRIVATE_KEY",
            "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
        )
        self.account = self.w3.eth.account.from_key(self.private_key)

        # Lightweight running totals for visibility
        self._initial_portfolio_value_token1 = None
        self._cumulative_gas_cost_eth = 0.0

        # Contract addresses
        self.position_manager_address = self.w3.to_checksum_address(CONTRACTS["position_manager"])
        self.pool_manager_address = self.w3.to_checksum_address(CONTRACTS["pool_manager"])
        self.token0_address = self.w3.to_checksum_address(CONTRACTS["token0"])
        self.token1_address = self.w3.to_checksum_address(CONTRACTS["token1"])
        self.hook_address = self.w3.to_checksum_address(CONTRACTS["hook"])
        self.permit2_address = self.w3.to_checksum_address(CONTRACTS["permit2"])

        # Pool constants
        self.fee = 3000
        self.tick_spacing = 60

        # Contracts
        self.posm = self.w3.eth.contract(address=self.position_manager_address, abi=POSITION_MANAGER_ABI)
        self.token0 = self.w3.eth.contract(address=self.token0_address, abi=ERC20_ABI)
        self.token1 = self.w3.eth.contract(address=self.token1_address, abi=ERC20_ABI)
        self.permit2 = self.w3.eth.contract(address=self.permit2_address, abi=PERMIT2_ABI)
        self.pool_manager = self.w3.eth.contract(address=self.pool_manager_address, abi=POOL_MANAGER_ABI)

        # Approve tokens and set Permit2 allowances for PositionManager
        self.approve_tokens()
        self.approve_permit2_for_posm()

        # MPPI Controller
        self.mppi = S_MPPI(
            PROPOSE=MPPI_CONFIG["PROPOSE"],
            horizon=MPPI_CONFIG["horizon"],
            num_samples=MPPI_CONFIG["num_samples"],
            num_samples_expect=MPPI_CONFIG["num_samples_expect"],
            dim_state=MPPI_CONFIG["dim_state"],
            dim_control=MPPI_CONFIG["dim_control"],
            dynamics=uniswap_dynamics,
            generate_random_parameter_seq=generate_jump_diffusion_parameter_seq,
            generate_constant_parameter_seq=generate_constant_parameter_seq,
            stage_cost=stage_cost,
            terminal_cost=terminal_cost,
            u_min=MPPI_CONFIG["u_min"],
            u_max=MPPI_CONFIG["u_max"],
            sigmas=MPPI_CONFIG["sigmas"],
            lambda_=MPPI_CONFIG["lambda_"],
            device=self.device,
            dtype=self.dtype,
        )

        print("MPPI Bot Initialized with Execution Capability", flush=True)

    # ------------------------------------------------------------------
    # Token approvals
    # ------------------------------------------------------------------

    def approve_tokens(self):
        """Approve ERC20 tokens for PositionManager (standard allowance)."""
        max_uint256 = 2**256 - 1
        try:
            nonce = self.w3.eth.get_transaction_count(self.account.address)
        except Exception as e:
            print(f"Failed to fetch nonce for approve_tokens: {e}", flush=True)
            return

        for token_addr in [self.token0_address, self.token1_address]:
            token = self.w3.eth.contract(address=token_addr, abi=ERC20_ABI)
            try:
                allowance = token.functions.allowance(self.account.address, self.position_manager_address).call()
                if allowance < max_uint256 // 2:
                    tx = token.functions.approve(self.position_manager_address, max_uint256).build_transaction({
                        "from": self.account.address,
                        "nonce": nonce,
                        "gasPrice": self.w3.eth.gas_price,
                    })
                    signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
                    tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                    self.w3.eth.wait_for_transaction_receipt(tx_hash)
                    print(f"Approved {token_addr} for PositionManager", flush=True)
                    nonce += 1
            except Exception as e:
                print(f"Approval error for {token_addr}: {e}", flush=True)

    def approve_permit2_for_posm(self):
        """Approve Permit2 allowances so PositionManager can pull tokens via Permit2."""
        max_amount = (1 << 160) - 1
        max_expiration = (1 << 48) - 1

        try:
            nonce = self.w3.eth.get_transaction_count(self.account.address)
        except Exception as e:
            print(f"Failed to fetch nonce for approve_permit2_for_posm: {e}", flush=True)
            return

        for token_addr in [self.token0_address, self.token1_address]:
            try:
                tx = self.permit2.functions.approve(
                    token_addr,
                    self.position_manager_address,
                    max_amount,
                    max_expiration,
                ).build_transaction({
                    "from": self.account.address,
                    "nonce": nonce,
                    "gasPrice": self.w3.eth.gas_price,
                })
                signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                self.w3.eth.wait_for_transaction_receipt(tx_hash)
                print(f"Permit2 approve: {token_addr} -> PositionManager", flush=True)
                nonce += 1
            except Exception as e:
                print(f"Permit2 approve error for {token_addr}: {e}", flush=True)

    # ------------------------------------------------------------------
    # Pool helpers
    # ------------------------------------------------------------------

    def price_to_tick(self, price):
        return int(math.log(price) / math.log(1.0001))

    def truncate_tick(self, tick):
        return (tick // self.tick_spacing) * self.tick_spacing

    def get_pool_key(self):
        """Return PoolKey tuple with tokens in canonical (address-sorted) order."""
        token0 = self.token0_address
        token1 = self.token1_address
        if token0.lower() > token1.lower():
            token0, token1 = token1, token0
        return (token0, token1, self.fee, self.tick_spacing, self.hook_address)

    def get_pool_id(self) -> bytes:
        return compute_pool_id(
            self.token0_address,
            self.token1_address,
            self.fee,
            self.tick_spacing,
            self.hook_address,
        )

    def get_deadline(self, offset_seconds: int = 600) -> int:
        latest_block = self.w3.eth.get_block("latest")
        return latest_block["timestamp"] + int(offset_seconds)

    # ------------------------------------------------------------------
    # State fetching
    # ------------------------------------------------------------------

    def fetch_current_state(self):
        """Fetch current state vector [t_market, t_pool, t_center, width_ticks]."""
        try:
            with sqlite3.connect(store.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT external_price, pool_price FROM price_history ORDER BY timestamp DESC LIMIT 1"
                )
                row = cursor.fetchone()

            if not row:
                return None

            ext_price, pool_price = row

            pool_id = self.get_pool_id()
            _, tick_lower, tick_upper = find_active_position(self.posm, pool_id)

            if tick_lower is None or tick_upper is None:
                t_pool = float(self.price_to_tick(pool_price))
                t_center = float(self.truncate_tick(int(round(t_pool))))
                width_ticks = float(4000)
            else:
                t_center = float(tick_lower + tick_upper) / 2.0
                width_ticks = float(tick_upper - tick_lower)

            t_market = float(self.price_to_tick(ext_price))
            t_pool_obs = float(self.price_to_tick(pool_price))

            return torch.tensor(
                [t_market, t_pool_obs, float(t_center), float(width_ticks)],
                dtype=self.dtype,
                device=self.device,
            )
        except Exception as e:
            print(f"Error fetching state: {e}", flush=True)
            return None

    # ------------------------------------------------------------------
    # Liquidity math
    # ------------------------------------------------------------------

    def calculate_max_liquidity(self, tick_lower, tick_upper, current_sqrt_price_x96, amount0, amount1):
        """Calculate max liquidity given balances and current price."""
        sqrt_price_current = current_sqrt_price_x96 / (2**96)
        sqrt_price_lower = 1.0001 ** (tick_lower / 2)
        sqrt_price_upper = 1.0001 ** (tick_upper / 2)

        if sqrt_price_lower > sqrt_price_upper:
            sqrt_price_lower, sqrt_price_upper = sqrt_price_upper, sqrt_price_lower

        if sqrt_price_current <= sqrt_price_lower:
            if sqrt_price_upper == sqrt_price_lower:
                return 0
            liquidity = amount0 * (sqrt_price_upper * sqrt_price_lower) / (sqrt_price_upper - sqrt_price_lower)
        elif sqrt_price_current >= sqrt_price_upper:
            if sqrt_price_upper == sqrt_price_lower:
                return 0
            liquidity = amount1 / (sqrt_price_upper - sqrt_price_lower)
        else:
            if sqrt_price_upper == sqrt_price_current or sqrt_price_current == sqrt_price_lower:
                return 0
            liquidity0 = amount0 * (sqrt_price_current * sqrt_price_upper) / (sqrt_price_upper - sqrt_price_current)
            liquidity1 = amount1 / (sqrt_price_current - sqrt_price_lower)
            liquidity = min(liquidity0, liquidity1)

        return int(liquidity)

    # ------------------------------------------------------------------
    # Transaction encoding (Uniswap v4 PositionManager)
    # ------------------------------------------------------------------

    def build_mint_unlock_data(
        self,
        pool_key,
        tick_lower: int,
        tick_upper: int,
        liquidity: int,
        amount0_max: int,
        amount1_max: int,
        recipient: str,
    ) -> bytes:
        """Build unlockData for Actions.MINT_POSITION path."""
        MINT_POSITION = 0x02
        SETTLE_PAIR = 0x0D
        SWEEP = 0x14

        actions = bytes([MINT_POSITION, SETTLE_PAIR, SWEEP, SWEEP])

        currency0 = pool_key[0]
        currency1 = pool_key[1]
        fee = int(pool_key[2])
        tick_spacing = int(pool_key[3])
        hooks = pool_key[4]

        params0 = encode(
            [
                "address", "address", "uint24", "int24", "address",
                "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes",
            ],
            [
                currency0, currency1, fee, tick_spacing, hooks,
                int(tick_lower), int(tick_upper), int(liquidity),
                int(amount0_max) & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF,
                int(amount1_max) & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF,
                recipient, b"",
            ],
        )
        params1 = encode(["address", "address"], [currency0, currency1])
        params2 = encode(["address", "address"], [currency0, recipient])
        params3 = encode(["address", "address"], [currency1, recipient])

        return encode(["bytes", "bytes[]"], [actions, [params0, params1, params2, params3]])

    def build_burn_unlock_data(self, token_id: int, recipient: str) -> bytes:
        """Build unlockData for Actions.BURN_POSITION path."""
        BURN_POSITION = 0x03
        TAKE_PAIR = 0x11

        actions = bytes([BURN_POSITION, TAKE_PAIR])

        pool_key, _ = self.posm.functions.getPoolAndPositionInfo(token_id).call()
        currency0 = pool_key[0]
        currency1 = pool_key[1]

        params0 = encode(
            ["uint256", "uint256", "uint256", "uint256", "bytes"],
            [int(token_id), 0, 0, 0, b""],
        )
        params1 = encode(["address", "address", "address"], [currency0, currency1, recipient])

        return encode(["bytes", "bytes[]"], [actions, [params0, params1]])

    # ------------------------------------------------------------------
    # On-chain execution
    # ------------------------------------------------------------------

    def execute_rebalance(self, new_lower_tick, new_upper_tick):
        """Execute rebalance: burn old position then mint new one."""
        try:
            pool_id = self.get_pool_id()
            old_lower_tick = None
            old_upper_tick = None
            gas_used_burn = 0
            cost_burn_eth = 0.0

            # 1. Burn old position
            old_token_id, old_lower_tick, old_upper_tick = find_active_position(self.posm, pool_id)
            if old_token_id is not None:
                liquidity = self.posm.functions.getPositionLiquidity(old_token_id).call()
                if liquidity > 0:
                    if old_lower_tick is not None:
                        print(
                            f"🔥 Burning old position: TokenID={old_token_id}, "
                            f"Range=[{old_lower_tick}, {old_upper_tick}], "
                            f"Liquidity={liquidity/1e18:.2f}",
                            flush=True,
                        )
                    else:
                        print(
                            f"🔥 Burning old position: TokenID={old_token_id}, Liquidity={liquidity/1e18:.2f}",
                            flush=True,
                        )

                    burn_unlock_data = self.build_burn_unlock_data(old_token_id, self.account.address)
                    tx_burn = self.posm.functions.modifyLiquidities(
                        burn_unlock_data,
                        self.get_deadline(365 * 24 * 3600),
                    ).build_transaction({
                        "from": self.account.address,
                        "nonce": self.w3.eth.get_transaction_count(self.account.address),
                        "gasPrice": self.w3.eth.gas_price,
                        "gas": 500000,
                    })
                    signed_burn = self.w3.eth.account.sign_transaction(tx_burn, self.private_key)
                    tx_hash = self.w3.eth.send_raw_transaction(signed_burn.raw_transaction)
                    receipt_burn = self.w3.eth.wait_for_transaction_receipt(tx_hash)
                    gas_used_burn = receipt_burn.gasUsed
                    gas_price_burn = (
                        receipt_burn.effectiveGasPrice
                        if hasattr(receipt_burn, "effectiveGasPrice")
                        else self.w3.eth.gas_price
                    )
                    cost_burn_eth = (gas_used_burn * gas_price_burn) / 1e18
                    print(f"   Burn confirmed. Gas: {gas_used_burn:,} | Cost: {cost_burn_eth:.6f} ETH", flush=True)

            # 2. Mint new position
            pool_key = self.get_pool_key()

            balance0 = self.token0.functions.balanceOf(self.account.address).call()
            balance1 = self.token1.functions.balanceOf(self.account.address).call()

            slot0_data = fetch_slot0(self.pool_manager, pool_id)
            if slot0_data is None:
                return False
            current_sqrt_price_x96 = slot0_data["sqrtPriceX96"]

            max_liquidity = self.calculate_max_liquidity(
                new_lower_tick, new_upper_tick, current_sqrt_price_x96, balance0, balance1
            )

            cap_e18 = float(os.getenv("MPPI_MAX_LIQUIDITY_E18", "2000"))
            liquidity_cap = int(cap_e18 * 1e18)
            liquidity_to_mint = min(int(max_liquidity * 0.70), liquidity_cap)

            if liquidity_to_mint == 0:
                try:
                    bal0 = self.token0.functions.balanceOf(self.account.address).call()
                    bal1 = self.token1.functions.balanceOf(self.account.address).call()
                    print(
                        "❌ Calculated liquidity is 0. Cannot mint.\n"
                        f"   MPPI account={self.account.address}\n"
                        f"   token0 balance={bal0/1e18:.6f}, token1 balance={bal1/1e18:.6f}\n"
                        f"   ticks=[{new_lower_tick}, {new_upper_tick}], sqrtPriceX96={current_sqrt_price_x96}",
                        flush=True,
                    )
                except Exception:
                    print("❌ Calculated liquidity is 0. Cannot mint (failed to read balances).", flush=True)
                return False

            mint_unlock_data = self.build_mint_unlock_data(
                pool_key, new_lower_tick, new_upper_tick, liquidity_to_mint,
                balance0, balance1, self.account.address,
            )

            print(
                f"🌱 Minting new position: TargetRange=[{new_lower_tick}, {new_upper_tick}], "
                f"Liquidity={liquidity_to_mint}",
                flush=True,
            )
            deadline_value = self.get_deadline(365 * 24 * 3600)
            tx_mint = self.posm.functions.modifyLiquidities(
                mint_unlock_data, deadline_value,
            ).build_transaction({
                "from": self.account.address,
                "nonce": self.w3.eth.get_transaction_count(self.account.address),
                "gasPrice": self.w3.eth.gas_price,
                "gas": 1_000_000,
            })

            signed_mint = self.w3.eth.account.sign_transaction(tx_mint, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_mint.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)

            gas_used_mint = receipt.gasUsed
            gas_price_mint = (
                receipt.effectiveGasPrice if hasattr(receipt, "effectiveGasPrice") else self.w3.eth.gas_price
            )
            cost_mint_eth = (gas_used_mint * gas_price_mint) / 1e18

            total_gas_used = gas_used_burn + gas_used_mint
            total_cost_eth = cost_burn_eth + cost_mint_eth

            if receipt.status == 1:
                print(f"✅ Rebalance successful! Tx: {tx_hash.hex()[:10]}...", flush=True)

                gas_price_gwei = gas_price_mint / 1e9 if isinstance(gas_price_mint, int) else 0
                cost_usd_estimate = total_cost_eth * 3000
                print(
                    f"   Gas: Burn={gas_used_burn:,} | Mint={gas_used_mint:,} | Total={total_gas_used:,} | "
                    f"Price={gas_price_gwei:.2f} gwei | Cost={total_cost_eth:.6f} ETH (~${cost_usd_estimate:.2f})",
                    flush=True,
                )

                try:
                    self._cumulative_gas_cost_eth += float(total_cost_eth)
                    pool_price = (current_sqrt_price_x96 / (2**96)) ** 2
                    bal0 = self.token0.functions.balanceOf(self.account.address).call()
                    bal1 = self.token1.functions.balanceOf(self.account.address).call()
                    v_token1 = (bal1 / 1e18) + (bal0 / 1e18) * float(pool_price)
                    if self._initial_portfolio_value_token1 is None:
                        self._initial_portfolio_value_token1 = v_token1
                    pnl_token1 = v_token1 - self._initial_portfolio_value_token1

                    print(
                        f"📊 MPPI PnL: value={v_token1:.4f} token1, PnL={pnl_token1:+.4f} token1, "
                        f"cumGas={self._cumulative_gas_cost_eth:.6f} ETH",
                        flush=True,
                    )

                    store.append_tx_event(
                        timestamp=time.time(),
                        actor="mppi",
                        event_type="rebalance",
                        tx_hash=tx_hash.hex(),
                        gas_used=int(total_gas_used),
                        gas_price_wei=int(gas_price_mint) if isinstance(gas_price_mint, int) else None,
                        cost_eth=float(total_cost_eth),
                        pool_price=float(pool_price),
                        portfolio_value_token1=float(v_token1),
                        portfolio_pnl_token1=float(pnl_token1),
                        meta={
                            "burn_gas_used": int(gas_used_burn),
                            "mint_gas_used": int(gas_used_mint),
                            "burn_cost_eth": float(cost_burn_eth),
                            "mint_cost_eth": float(cost_mint_eth),
                            "cumulative_gas_cost_eth": float(self._cumulative_gas_cost_eth),
                            "new_lower_tick": int(new_lower_tick),
                            "new_upper_tick": int(new_upper_tick),
                        },
                    )
                except Exception:
                    pass

                # Verify new position was created
                try:
                    new_next_token_id = self.posm.functions.nextTokenId().call()
                    if new_next_token_id > 1:
                        token_id = new_next_token_id - 1
                        _, info = self.posm.functions.getPoolAndPositionInfo(token_id).call()
                        new_lower_decoded, new_upper_decoded = decode_position_ticks(int(info))
                        position_liquidity = self.posm.functions.getPositionLiquidity(token_id).call()
                        if position_liquidity > 0:
                            print(
                                f"   ✅ Verified: New position TokenID={token_id} "
                                f"Range=[{new_lower_decoded}, {new_upper_decoded}] "
                                f"Liquidity={position_liquidity/1e18:.2f}",
                                flush=True,
                            )
                            if old_lower_tick is not None and old_upper_tick is not None:
                                print(
                                    f"   Range update: "
                                    f"[{old_lower_tick}, {old_upper_tick}] -> "
                                    f"[{new_lower_decoded}, {new_upper_decoded}]",
                                    flush=True,
                                )
                        else:
                            print(
                                f"   ⚠️ Warning: Position TokenID={token_id} exists but has 0 liquidity",
                                flush=True,
                            )
                except Exception as verify_err:
                    print(f"   ⚠️ Could not verify position creation: {verify_err}", flush=True)
            else:
                print(f"❌ Rebalance failed! Tx: {tx_hash.hex()[:10]}...", flush=True)
                block = self.w3.eth.get_block(receipt.blockNumber)
                block_timestamp = block["timestamp"]
                print(
                    f"DEBUG: Block timestamp={block_timestamp}, deadline={deadline_value}, "
                    f"diff={block_timestamp - deadline_value}",
                    flush=True,
                )
                if block_timestamp > deadline_value:
                    print(
                        f"ERROR: Block timestamp ({block_timestamp}) > deadline ({deadline_value})! "
                        "This is why DeadlinePassed occurred.",
                        flush=True,
                    )
                else:
                    print(
                        "DEBUG: Block timestamp is OK (not past deadline), but still got DeadlinePassed.",
                        flush=True,
                    )

                try:
                    tx = self.w3.eth.get_transaction(tx_hash)
                    tx_dict = dict(tx)
                    for field in ["hash", "nonce", "blockHash", "blockNumber", "transactionIndex", "v", "r", "s"]:
                        tx_dict.pop(field, None)
                    try:
                        self.w3.eth.call(tx_dict, receipt.blockNumber)
                    except Exception as call_error:
                        error_str = str(call_error)
                        print(f"   Replay error: {error_str}", flush=True)

                        error_data = None
                        if hasattr(call_error, "args") and len(call_error.args) > 0:
                            if isinstance(call_error.args[0], (tuple, list)):
                                error_data = call_error.args[0][0] if call_error.args[0] else None
                            elif isinstance(call_error.args[0], str):
                                error_data = call_error.args[0]
                            elif isinstance(call_error.args[0], dict):
                                error_data = call_error.args[0].get("data")

                        if error_data is None:
                            import re
                            matches = re.findall(r"0x[0-9a-fA-F]{64,}", error_str)
                            if matches:
                                error_data = matches[0]

                        if error_data and isinstance(error_data, str) and error_data.startswith("0x"):
                            selector = error_data[:10]
                            print(f"   Extracted error selector: {selector}", flush=True)
                            known_errors = {
                                "0xbfb22adf": "DeadlinePassed(uint256)",
                                "0x0ca968d8": "NotApproved(address)",
                                "0xd4b05fe0": "PoolManagerMustBeLocked()",
                                "0x3b99b53d": "SliceOutOfBounds()",
                                "0xaaad13f7": "InputLengthMismatch()",
                                "0x5cda29d7": "UnsupportedAction(uint256)",
                            }
                            if selector in known_errors:
                                print(f"   ✅ Identified error: {known_errors[selector]}", flush=True)
                                if selector == "0xbfb22adf" and len(error_data) >= 74:
                                    print(f"   Deadline in error: {int(error_data[10:74], 16)}", flush=True)
                            else:
                                print(f"   ⚠️ Unknown error selector: {selector}", flush=True)
                                print(f"   Full error data: {error_data}", flush=True)
                                if len(error_data) >= 74:
                                    try:
                                        decoded_value = int(error_data[10:74], 16)
                                        print(f"   Decoded uint256 value: {decoded_value}", flush=True)
                                    except Exception:
                                        pass
                        else:
                            print(f"   Could not extract error data from: {call_error}", flush=True)
                except Exception as trace_err:
                    print(f"   Failed to trace transaction: {trace_err}", flush=True)

        except Exception as e:
            print(f"Rebalance execution error: {e}", flush=True)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        print("Starting MPPI Bot Loop...", flush=True)
        while True:
            try:
                state = self.fetch_current_state()
                if state is None:
                    print("Waiting for data...", flush=True)
                    time.sleep(2)
                    continue

                action, _ = self.mppi.forward(state)

                delta_center = float(action[0].item())
                delta_width = float(action[1].item())

                current_center = float(state[2].item())
                current_width = max(float(self.tick_spacing * 2), float(state[3].item()))

                current_lower_tick = self.truncate_tick(int(round(current_center - current_width / 2.0)))
                current_upper_tick = self.truncate_tick(int(round(current_center + current_width / 2.0)))
                if current_lower_tick >= current_upper_tick:
                    current_upper_tick = current_lower_tick + self.tick_spacing

                if abs(delta_center) > 0 or abs(delta_width) > 0:
                    new_center = current_center + delta_center
                    new_width = max(float(self.tick_spacing * 2), current_width + delta_width)

                    new_lower_tick = self.truncate_tick(int(round(new_center - new_width / 2.0)))
                    new_upper_tick = self.truncate_tick(int(round(new_center + new_width / 2.0)))
                    if new_lower_tick >= new_upper_tick:
                        new_upper_tick = new_lower_tick + self.tick_spacing

                    print(f"🚀 MPPI Rebalance Proposed:", flush=True)
                    print(
                        f"   State(ticks): t_mkt={state[0].item():.1f}, t_pool={state[1].item():.1f}, "
                        f"t_center={current_center:.1f}, w={current_width:.1f}",
                        flush=True,
                    )
                    print(f"   Control(ticks): Δcenter={delta_center:.1f}, Δw={delta_width:.1f}", flush=True)
                    print(
                        f"   Current Range: Ticks=[{current_lower_tick}, {current_upper_tick}] | "
                        f"Price=[{1.0001**current_lower_tick:.2f}, {1.0001**current_upper_tick:.2f}]",
                        flush=True,
                    )
                    print(
                        f"   Target Range:  Ticks=[{new_lower_tick}, {new_upper_tick}] | "
                        f"Price=[{1.0001**new_lower_tick:.2f}, {1.0001**new_upper_tick:.2f}]",
                        flush=True,
                    )
                    print(
                        f"   Tick Change: Lower={new_lower_tick - current_lower_tick:+d}, "
                        f"Upper={new_upper_tick - current_upper_tick:+d}",
                        flush=True,
                    )

                    self.execute_rebalance(new_lower_tick, new_upper_tick)

                time.sleep(3)

            except Exception as e:
                print(f"MPPI Bot Error: {e}", flush=True)
                time.sleep(10)


if __name__ == "__main__":
    bot = MPPIBot()
    bot.run()
