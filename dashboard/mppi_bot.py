import time
import torch
import sqlite3
import numpy as np
import os
import math
import sys

# Add project root to sys.path to ensure imports work correctly
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web3 import Web3
from dotenv import load_dotenv
from eth_abi import encode

from dashboard.optimizer.controller import S_MPPI
from dashboard.optimizer.config_for_mppi import MPPI_CONFIG
from dashboard.optimizer.cost_function import stage_cost, terminal_cost
from dashboard.optimizer.utils import uniswap_dynamics, generate_jump_diffusion_parameter_seq, generate_constant_parameter_seq
from dashboard.data_store import store
from dashboard.config import CONTRACTS

load_dotenv()

class MPPIBot:
    def __init__(self):
        self.device = torch.device("cpu")
        self.dtype = torch.float32
        
        # Web3 Setup
        self.rpc_url = os.getenv("ANVIL_RPC_URL", "http://127.0.0.1:8545")
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        
        self.private_key = os.getenv(
            "BOT_PRIVATE_KEY",
            "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
        )
        self.account = self.w3.eth.account.from_key(self.private_key)
        
        # Contract Addresses
        self.position_manager_address = self.w3.to_checksum_address(CONTRACTS['position_manager'])
        self.pool_manager_address = self.w3.to_checksum_address(CONTRACTS['pool_manager'])
        self.token0_address = self.w3.to_checksum_address(CONTRACTS['token0'])
        self.token1_address = self.w3.to_checksum_address(CONTRACTS['token1'])
        self.hook_address = self.w3.to_checksum_address(CONTRACTS['hook'])
        self.permit2_address = self.w3.to_checksum_address(CONTRACTS['permit2'])
        
        # Pool Constants
        self.fee = 3000
        self.tick_spacing = 60
        
        # ABI for PositionManager (subset of IPositionManager)
        # IMPORTANT: modifyLiquidities signature: modifyLiquidities(bytes calldata unlockData, uint256 deadline)
        self.posm_abi = [
            {
                "inputs": [
                    {"internalType": "bytes", "name": "unlockData", "type": "bytes"},
                    {"internalType": "uint256", "name": "deadline", "type": "uint256"},
                ],
                "name": "modifyLiquidities",
                "outputs": [],
                "stateMutability": "payable",
                "type": "function",
            },
            {
                "inputs": [],
                "name": "nextTokenId",
                "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}],
                "name": "getPoolAndPositionInfo",
                "outputs": [
                    {
                        "components": [
                            {"internalType": "address", "name": "currency0", "type": "address"},
                            {"internalType": "address", "name": "currency1", "type": "address"},
                            {"internalType": "uint24", "name": "fee", "type": "uint24"},
                            {"internalType": "int24", "name": "tickSpacing", "type": "int24"},
                            {"internalType": "address", "name": "hooks", "type": "address"},
                        ],
                        "internalType": "struct PoolKey",
                        "name": "poolKey",
                        "type": "tuple",
                    },
                    {"internalType": "uint256", "name": "info", "type": "uint256"},
                ],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}],
                "name": "getPositionLiquidity",
                "outputs": [{"internalType": "uint128", "name": "liquidity", "type": "uint128"}],
                "stateMutability": "view",
                "type": "function",
            },
        ]
        
        self.posm = self.w3.eth.contract(address=self.position_manager_address, abi=self.posm_abi)
        
        # ERC20 ABI
        self.erc20_abi = [
            {
                "constant": False,
                "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
                "name": "approve",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function"
            },
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}],
                "name": "allowance",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function"
            },
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function"
            }
        ]
        
        # Initialize Token Contracts
        self.token0 = self.w3.eth.contract(address=self.token0_address, abi=self.erc20_abi)
        self.token1 = self.w3.eth.contract(address=self.token1_address, abi=self.erc20_abi)

        # Minimal Permit2 ABI (approve only)
        self.permit2_abi = [
            {
                "inputs": [
                    {"internalType": "address", "name": "token", "type": "address"},
                    {"internalType": "address", "name": "spender", "type": "address"},
                    {"internalType": "uint160", "name": "amount", "type": "uint160"},
                    {"internalType": "uint48", "name": "expiration", "type": "uint48"},
                ],
                "name": "approve",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function",
            },
        ]
        self.permit2 = self.w3.eth.contract(address=self.permit2_address, abi=self.permit2_abi)
        
        # Initialize PoolManager Contract
        self.pool_manager_abi = [
            {"inputs": [{"internalType": "bytes32", "name": "poolId", "type": "bytes32"}], "name": "getSlot0", "outputs": [{"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"}, {"internalType": "int24", "name": "tick", "type": "int24"}, {"internalType": "uint16", "name": "protocolFee", "type": "uint16"}, {"internalType": "uint16", "name": "liquidity", "type": "uint16"}], "stateMutability": "view", "type": "function"},
            {
                "inputs": [{"internalType": "bytes32", "name": "slot", "type": "bytes32"}],
                "name": "extsload",
                "outputs": [{"internalType": "bytes32", "name": "value", "type": "bytes32"}],
                "stateMutability": "view",
                "type": "function"
            }
        ]
        self.pool_manager = self.w3.eth.contract(address=self.pool_manager_address, abi=self.pool_manager_abi)

        # Approve tokens and set Permit2 allowances for PositionManager
        self.approve_tokens()
        self.approve_permit2_for_posm()

        # MPPI Controller Initialization
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
            dtype=self.dtype
        )
        
        print("MPPI Bot Initialized with Execution Capability", flush=True)

    def approve_tokens(self):
        """Approve ERC20 tokens for PositionManager (standard allowance)"""
        max_uint256 = 2**256 - 1
        try:
            nonce = self.w3.eth.get_transaction_count(self.account.address)
        except Exception as e:
            print(f"Failed to fetch nonce for approve_tokens: {e}", flush=True)
            return

        for token_addr in [self.token0_address, self.token1_address]:
            token = self.w3.eth.contract(address=token_addr, abi=self.erc20_abi)
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
        """Approve Permit2 allowances so PositionManager can pull tokens via Permit2"""
        # Match AnvilRun.s.sol: use max uint160 / max uint48
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
                ).build_transaction(
                    {
                        "from": self.account.address,
                        "nonce": nonce,
                        "gasPrice": self.w3.eth.gas_price,
                    }
                )
                signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                self.w3.eth.wait_for_transaction_receipt(tx_hash)
                print(f"Permit2 approve: {token_addr} -> PositionManager", flush=True)
                nonce += 1
            except Exception as e:
                print(f"Permit2 approve error for {token_addr}: {e}", flush=True)

    def price_to_tick(self, price):
        return int(math.log(price) / math.log(1.0001))

    def truncate_tick(self, tick):
        return (tick // self.tick_spacing) * self.tick_spacing

    def get_pool_key(self):
        """Construct PoolKey ensuring correct token order"""
        token0 = self.token0_address
        token1 = self.token1_address
        if token0.lower() > token1.lower():
            token0, token1 = token1, token0
            
        return (
            token0,
            token1,
            self.fee,
            self.tick_spacing,
            self.hook_address
        )

    def get_pool_id(self):
        pool_key_tuple = self.get_pool_key()
        pool_key_encoded = encode(
            ['address', 'address', 'uint24', 'int24', 'address'],
            pool_key_tuple
        )
        return Web3.keccak(pool_key_encoded)

    def fetch_current_state(self):
        """Fetch current state: [external_price, pool_price, range_center, range_width]"""
        try:
            # Get external_price and pool_price from database
            with sqlite3.connect(store.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT external_price, pool_price 
                    FROM price_history 
                    ORDER BY timestamp DESC LIMIT 1
                """)
                row = cursor.fetchone()
                
            if not row:
                return None
                
            ext_price, pool_price = row
            
            # Get actual range from PositionManager (not from database)
            tick_lower, tick_upper = self._fetch_current_range()
            
            if tick_lower is None or tick_upper is None:
                # Fallback: use pool_price ± 20% if no position found
                price_lower = pool_price * 0.8
                price_upper = pool_price * 1.2
            else:
                # Convert ticks to prices
                price_lower = 1.0001 ** tick_lower
                price_upper = 1.0001 ** tick_upper
            
            p_center = (price_upper + price_lower) / 2.0
            width = price_upper - price_lower
            
            state = torch.tensor([ext_price, pool_price, p_center, width], dtype=self.dtype, device=self.device)
            return state
            
        except Exception as e:
            print(f"Error fetching state: {e}", flush=True)
            return None
    
    def _fetch_current_range(self):
        """Fetch current tick range from PositionManager for active position"""
        try:
            next_token_id = self.posm.functions.nextTokenId().call()
            pool_id = self.get_pool_id()
            
            # Check last 10 tokens to find active position
            for i in range(10):
                token_id = next_token_id - 1 - i
                if token_id < 1:
                    break
                
                try:
                    pool_key, position_info = self.posm.functions.getPoolAndPositionInfo(token_id).call()
                    liquidity = self.posm.functions.getPositionLiquidity(token_id).call()
                    
                    if liquidity > 0:
                        # Verify pool matches
                        pool_key_tuple = (pool_key[0], pool_key[1], pool_key[2], pool_key[3], pool_key[4])
                        encoded = encode(['address', 'address', 'uint24', 'int24', 'address'], pool_key_tuple)
                        if Web3.keccak(encoded) == pool_id:
                            # Decode PositionInfo: 200 bits poolId | 24 bits tickUpper | 24 bits tickLower | 8 bits hasSubscriber
                            info_int = int(position_info)
                            tick_lower_raw = (info_int >> 8) & 0xFFFFFF
                            tick_upper_raw = (info_int >> 32) & 0xFFFFFF
                            
                            # Sign extend int24
                            if tick_lower_raw & (1 << 23):
                                tick_lower = tick_lower_raw - (1 << 24)
                            else:
                                tick_lower = tick_lower_raw
                            
                            if tick_upper_raw & (1 << 23):
                                tick_upper = tick_upper_raw - (1 << 24)
                            else:
                                tick_upper = tick_upper_raw
                            
                            return tick_lower, tick_upper
                except:
                    continue
            
            return None, None
        except Exception as e:
            return None, None

    def calculate_max_liquidity(self, tick_lower, tick_upper, current_sqrt_price_x96, amount0, amount1):
        """Calculate max liquidity given balances and current price"""
        sqrt_price_current = current_sqrt_price_x96 / (2**96)
        sqrt_price_lower = 1.0001 ** (tick_lower / 2)
        sqrt_price_upper = 1.0001 ** (tick_upper / 2)
        
        # Ensure lower < upper
        if sqrt_price_lower > sqrt_price_upper:
            sqrt_price_lower, sqrt_price_upper = sqrt_price_upper, sqrt_price_lower
            
        if sqrt_price_current <= sqrt_price_lower:
            # Current price below range: only Token0 needed
            if sqrt_price_upper == sqrt_price_lower: return 0
            liquidity = amount0 * (sqrt_price_upper * sqrt_price_lower) / (sqrt_price_upper - sqrt_price_lower)
        elif sqrt_price_current >= sqrt_price_upper:
            # Current price above range: only Token1 needed
            if sqrt_price_upper == sqrt_price_lower: return 0
            liquidity = amount1 / (sqrt_price_upper - sqrt_price_lower)
        else:
            # In range: both needed
            if sqrt_price_upper == sqrt_price_current or sqrt_price_current == sqrt_price_lower: return 0
            
            liquidity0 = amount0 * (sqrt_price_current * sqrt_price_upper) / (sqrt_price_upper - sqrt_price_current)
            liquidity1 = amount1 / (sqrt_price_current - sqrt_price_lower)
            
            liquidity = min(liquidity0, liquidity1)
            
        return int(liquidity)

    def get_deadline(self, offset_seconds: int = 600) -> int:
        """Get deadline based on chain time (block.timestamp) plus offset"""
        latest_block = self.w3.eth.get_block("latest")
        chain_now = latest_block["timestamp"]
        deadline = chain_now + int(offset_seconds)
        return deadline
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
        """
        Build unlockData for Actions.MINT_POSITION path used by EasyPosm.mint:
        actions = [MINT_POSITION, SETTLE_PAIR, SWEEP, SWEEP]
        params[0] = abi.encode(poolKey, tickLower, tickUpper, liquidity, amount0Max, amount1Max, recipient, hookData)
        params[1] = abi.encode(currency0, currency1)
        params[2] = abi.encode(currency0, recipient)
        params[3] = abi.encode(currency1, recipient)
        """
        # Action codes from Actions.sol
        MINT_POSITION = 0x02
        SETTLE_PAIR = 0x0D
        SWEEP = 0x14

        actions = bytes([MINT_POSITION, SETTLE_PAIR, SWEEP, SWEEP])

        currency0 = pool_key[0]
        currency1 = pool_key[1]
        fee = int(pool_key[2])
        tick_spacing = int(pool_key[3])
        hooks = pool_key[4]

        # Encode params[0] matching decodeMintParams:
        # (PoolKey, int24, int24, uint256, uint128, uint128, address, bytes)
        # PoolKey is a struct: (address currency0, address currency1, uint24 fee, int24 tickSpacing, address hooks)
        # Note: amount0Max and amount1Max must be uint128, not uint256
        params0 = encode(
            [
                "address",  # currency0
                "address",  # currency1
                "uint24",   # fee
                "int24",    # tickSpacing
                "address",  # hooks
                "int24",    # tickLower
                "int24",    # tickUpper
                "uint256",  # liquidity
                "uint128",  # amount0Max (changed from uint256)
                "uint128",  # amount1Max (changed from uint256)
                "address",  # owner/recipient
                "bytes",    # hookData
            ],
            [
                currency0,
                currency1,
                fee,
                tick_spacing,
                hooks,
                int(tick_lower),
                int(tick_upper),
                int(liquidity),
                int(amount0_max) & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF,  # Ensure uint128 range
                int(amount1_max) & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF,  # Ensure uint128 range
                recipient,
                b"",
            ],
        )

        params1 = encode(["address", "address"], [currency0, currency1])
        params2 = encode(["address", "address"], [currency0, recipient])
        params3 = encode(["address", "address"], [currency1, recipient])

        unlock_data = encode(["bytes", "bytes[]"], [actions, [params0, params1, params2, params3]])
        return unlock_data

    def build_burn_unlock_data(self, token_id: int, recipient: str) -> bytes:
        """
        Build unlockData for Actions.BURN_POSITION path used by EasyPosm.burn:
        actions = [BURN_POSITION, TAKE_PAIR]
        params[0] = abi.encode(tokenId, 0, amount0Min, amount1Min, hookData)
        params[1] = abi.encode(currency0, currency1, recipient)
        """
        # Action codes from Actions.sol
        BURN_POSITION = 0x03
        TAKE_PAIR = 0x11

        actions = bytes([BURN_POSITION, TAKE_PAIR])

        # Fetch pool key to get currencies
        pool_key, _ = self.posm.functions.getPoolAndPositionInfo(token_id).call()
        currency0 = pool_key[0]
        currency1 = pool_key[1]

        amount0_min = 0
        amount1_min = 0

        params0 = encode(
            ["uint256", "uint256", "uint256", "uint256", "bytes"],
            [int(token_id), 0, amount0_min, amount1_min, b""],
        )
        params1 = encode(
            ["address", "address", "address"],
            [currency0, currency1, recipient],
        )

        unlock_data = encode(["bytes", "bytes[]"], [actions, [params0, params1]])
        return unlock_data

    def find_active_position(self):
        """Find the most recent active position token ID for this pool"""
        try:
            next_token_id = self.posm.functions.nextTokenId().call()
            pool_id = self.get_pool_id()
            
            # Check last 10 tokens
            for i in range(10):
                token_id = next_token_id - 1 - i
                if token_id < 1:
                    break
                
                try:
                    pool_key, _ = self.posm.functions.getPoolAndPositionInfo(token_id).call()
                    liquidity = self.posm.functions.getPositionLiquidity(token_id).call()
                    
                    if liquidity > 0:
                        # Reconstruct pool key to verify pool
                        pool_key_tuple = (
                            pool_key[0], pool_key[1], pool_key[2], pool_key[3], pool_key[4]
                        )
                        encoded = encode(['address', 'address', 'uint24', 'int24', 'address'], pool_key_tuple)
                        if Web3.keccak(encoded) == pool_id:
                            return token_id
                except:
                    continue
            return None
        except Exception as e:
            print(f"Error finding position: {e}", flush=True)
            return None

    def execute_rebalance(self, new_lower_tick, new_upper_tick):
        """Execute rebalance: Burn old position, Mint new position"""
        try:
            # Helper to decode int24 ticks from packed PositionInfo
            def _decode_tick(info_int: int, shift: int) -> int:
                raw = (info_int >> shift) & 0xFFFFFF  # 24 bits
                if raw & 0x800000:  # negative number
                    raw -= 1 << 24
                return raw

            old_lower_tick = None
            old_upper_tick = None
            gas_used_burn = 0
            cost_burn_eth = 0

            # 1. Burn old position via modifyLiquidities (EasyPosm.burn equivalent)
            old_token_id = self.find_active_position()
            if old_token_id:
                liquidity = self.posm.functions.getPositionLiquidity(old_token_id).call()
                if liquidity > 0:
                    # Decode previous range from PositionInfo for logging
                    try:
                        _, info = self.posm.functions.getPoolAndPositionInfo(old_token_id).call()
                        info_int = int(info)
                        old_lower_tick = _decode_tick(info_int, 8)
                        old_upper_tick = _decode_tick(info_int, 32)
                        print(
                            f"🔥 Burning old position: TokenID={old_token_id}, "
                            f"Range=[{old_lower_tick}, {old_upper_tick}], "
                            f"Liquidity={liquidity/1e18:.2f}",
                            flush=True,
                        )
                    except Exception:
                        print(
                            f"🔥 Burning old position: TokenID={old_token_id}, Liquidity={liquidity/1e18:.2f}",
                            flush=True,
                        )

                    burn_unlock_data = self.build_burn_unlock_data(old_token_id, self.account.address)
                    tx_burn = self.posm.functions.modifyLiquidities(
                        burn_unlock_data,
                        self.get_deadline(365 * 24 * 3600),  # 1 year from now on chain
                    ).build_transaction(
                        {
                            "from": self.account.address,
                            "nonce": self.w3.eth.get_transaction_count(self.account.address),
                            "gasPrice": self.w3.eth.gas_price,
                            "gas": 500000,  # Optimized: typical burn uses 200k-400k gas
                        }
                    )
                    signed_burn = self.w3.eth.account.sign_transaction(tx_burn, self.private_key)
                    tx_hash = self.w3.eth.send_raw_transaction(signed_burn.raw_transaction)
                    receipt_burn = self.w3.eth.wait_for_transaction_receipt(tx_hash)
                    gas_used_burn = receipt_burn.gasUsed
                    gas_price_burn = receipt_burn.effectiveGasPrice if hasattr(receipt_burn, 'effectiveGasPrice') else self.w3.eth.gas_price
                    cost_burn_wei = gas_used_burn * gas_price_burn
                    cost_burn_eth = cost_burn_wei / 1e18
                    print(f"   Burn confirmed. Gas: {gas_used_burn:,} | Cost: {cost_burn_eth:.6f} ETH", flush=True)

            # 2. Mint new position via modifyLiquidities (EasyPosm.mint equivalent)
            pool_key = self.get_pool_key()

            balance0 = self.token0.functions.balanceOf(self.account.address).call()
            balance1 = self.token1.functions.balanceOf(self.account.address).call()

            # Get current price for liquidity calculation
            pool_id = self.get_pool_id()

            try:
                pools_slot = b"\x00" * 31 + b"\x06"
                slot = Web3.keccak(pool_id + pools_slot)
                value = self.pool_manager.functions.extsload(slot).call()
                data = int.from_bytes(value, byteorder="big")
                current_sqrt_price_x96 = data & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
            except Exception as e:
                try:
                    slot0 = self.pool_manager.functions.getSlot0(pool_id).call()
                    current_sqrt_price_x96 = slot0[0]
                except Exception as e2:
                    return False

            max_liquidity = self.calculate_max_liquidity(
                new_lower_tick,
                new_upper_tick,
                current_sqrt_price_x96,
                balance0,
                balance1,
            )

            # Reserve some balance for arbitrage bot (use 70% instead of 95%)
            # This prevents "Insufficient balance" errors in arbitrage bot
            liquidity_to_mint = int(max_liquidity * 0.70)

            if liquidity_to_mint == 0:
                print("❌ Calculated liquidity is 0. Cannot mint.", flush=True)
                return False

            mint_unlock_data = self.build_mint_unlock_data(
                pool_key,
                new_lower_tick,
                new_upper_tick,
                liquidity_to_mint,
                balance0,
                balance1,
                self.account.address,
            )

            print(
                f"🌱 Minting new position: TargetRange=[{new_lower_tick}, {new_upper_tick}], "
                f"Liquidity={liquidity_to_mint}",
                flush=True,
            )
            deadline_value = self.get_deadline(365 * 24 * 3600)  # 1 year from now on chain
            # Build and send mint transaction
            tx_mint = self.posm.functions.modifyLiquidities(
                mint_unlock_data,
                deadline_value,
            ).build_transaction(
                {
                    "from": self.account.address,
                    "nonce": self.w3.eth.get_transaction_count(self.account.address),
                    "gasPrice": self.w3.eth.gas_price,
                    "gas": 1_000_000,  # Optimized: typical mint uses 300k-600k gas (1M is safe buffer)
                }
            )

            signed_mint = self.w3.eth.account.sign_transaction(tx_mint, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_mint.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
            
            # Calculate actual gas cost
            gas_used_mint = receipt.gasUsed
            gas_price_mint = receipt.effectiveGasPrice if hasattr(receipt, 'effectiveGasPrice') else self.w3.eth.gas_price
            cost_mint_wei = gas_used_mint * gas_price_mint
            cost_mint_eth = cost_mint_wei / 1e18
            
            # Total cost (burn + mint)
            total_gas_used = gas_used_burn + gas_used_mint if 'gas_used_burn' in locals() else gas_used_mint
            total_cost_eth = cost_burn_eth + cost_mint_eth if 'cost_burn_eth' in locals() else cost_mint_eth
            
            if receipt.status == 1:
                # Verify success by checking transaction receipt status and position creation
                print(f"✅ Rebalance successful! Tx: {tx_hash.hex()[:10]}...", flush=True)
                
                # Calculate cost in USD (Base Sepolia: ~0.1 gwei, Base Mainnet: ~0.1 gwei)
                gas_price_gwei = gas_price_mint / 1e9 if isinstance(gas_price_mint, int) else 0
                cost_usd_estimate = total_cost_eth * 3000  # Rough USD estimate (ETH ~$3000)
                
                print(
                    f"   Gas: Burn={gas_used_burn:,} | Mint={gas_used_mint:,} | Total={total_gas_used:,} | "
                    f"Price={gas_price_gwei:.2f} gwei | Cost={total_cost_eth:.6f} ETH (~${cost_usd_estimate:.2f})",
                    flush=True
                )
                
                # Additional verification: Check if new position was created
                try:
                    new_next_token_id = self.posm.functions.nextTokenId().call()
                    if new_next_token_id > 1:
                        token_id = new_next_token_id - 1
                        _, info = self.posm.functions.getPoolAndPositionInfo(token_id).call()
                        info_int = int(info)
                        new_lower_decoded = _decode_tick(info_int, 8)
                        new_upper_decoded = _decode_tick(info_int, 32)
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
                            print(f"   ⚠️ Warning: Position TokenID={token_id} exists but has 0 liquidity", flush=True)
                except Exception as verify_err:
                    print(f"   ⚠️ Could not verify position creation: {verify_err}", flush=True)
            else:
                print(f"❌ Rebalance failed! Tx: {tx_hash.hex()[:10]}...", flush=True)
                # Check block timestamp vs deadline
                block = self.w3.eth.get_block(receipt.blockNumber)
                block_timestamp = block['timestamp']
                print(f"DEBUG: Block timestamp={block_timestamp}, deadline={deadline_value}, diff={block_timestamp - deadline_value}", flush=True)
                if block_timestamp > deadline_value:
                    print(f"ERROR: Block timestamp ({block_timestamp}) > deadline ({deadline_value})! This is why DeadlinePassed occurred.", flush=True)
                else:
                    print(f"DEBUG: Block timestamp is OK (not past deadline), but still got DeadlinePassed. Investigating...", flush=True)
                
                # Try to get revert reason by replaying the transaction
                try:
                    tx = self.w3.eth.get_transaction(tx_hash)
                    tx_dict = dict(tx)
                    # Remove fields that shouldn't be in call
                    fields_to_remove = ['hash', 'nonce', 'blockHash', 'blockNumber', 'transactionIndex', 'v', 'r', 's']
                    for field in fields_to_remove:
                        if field in tx_dict:
                            del tx_dict[field]
                    
                    # Replay the transaction to get the actual revert reason
                    try:
                        self.w3.eth.call(tx_dict, receipt.blockNumber)
                    except Exception as call_error:
                        # Extract error data from the exception
                        error_str = str(call_error)
                        print(f"   Replay error: {error_str}", flush=True)
                        
                        # Try to extract error selector from the error message
                        error_data = None
                        if hasattr(call_error, 'args') and len(call_error.args) > 0:
                            # Handle tuple/list format: ('0xd81b2f2e...', '0xd81b2f2e...')
                            if isinstance(call_error.args[0], (tuple, list)):
                                error_data = call_error.args[0][0] if len(call_error.args[0]) > 0 else None
                            elif isinstance(call_error.args[0], str):
                                error_data = call_error.args[0]
                            elif isinstance(call_error.args[0], dict):
                                # Try to get 'data' field from dict
                                error_data = call_error.args[0].get('data', None)
                        
                        # Also try to extract from error string directly
                        if error_data is None:
                            import re
                            hex_pattern = r'0x[0-9a-fA-F]{64,}'
                            matches = re.findall(hex_pattern, error_str)
                            if matches:
                                error_data = matches[0]
                        
                        if error_data and isinstance(error_data, str) and error_data.startswith("0x"):
                            selector = error_data[:10]
                            print(f"   Extracted error selector: {selector}", flush=True)
                            
                            # Check if this matches known errors
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
                                if selector == "0xbfb22adf":  # DeadlinePassed(uint256)
                                    if len(error_data) >= 74:
                                        deadline_in_error = int(error_data[10:74], 16)
                                        print(f"   Deadline in error: {deadline_in_error}", flush=True)
                            else:
                                print(f"   ⚠️ Unknown error selector: {selector}", flush=True)
                                print(f"   Full error data: {error_data}", flush=True)
                                
                                # Try to decode as uint256 to see what value it contains
                                if len(error_data) >= 74:
                                    try:
                                        decoded_value = int(error_data[10:74], 16)
                                        print(f"   Decoded uint256 value: {decoded_value}", flush=True)
                                        if decoded_value == 0:
                                            print(f"   ⚠️ Error parameter is 0 - this might indicate a parameter encoding issue", flush=True)
                                    except:
                                        pass
                        else:
                            print(f"   Could not extract error data from: {call_error}", flush=True)
                        
                except Exception as trace_err:
                    print(f"   Failed to trace transaction: {trace_err}", flush=True)

        except Exception as e:
            print(f"Rebalance execution error: {e}", flush=True)

    def run(self):
        print("Starting MPPI Bot Loop...", flush=True)
        while True:
            try:
                state = self.fetch_current_state()
                if state is None:
                    print("Waiting for data...", flush=True)
                    time.sleep(2)
                    continue
                
                # Execute MPPI Control
                action, _ = self.mppi.forward(state)
                
                delta_center = action[0].item()
                delta_width = action[1].item()
                
                if abs(delta_center) > 0 or abs(delta_width) > 0:
                    current_center = state[2].item()
                    current_width = state[3].item()
                    
                    new_center = current_center + delta_center
                    new_width = current_width + delta_width
                    
                    new_lower_price = new_center - (new_width / 2)
                    new_upper_price = new_center + (new_width / 2)
                    
                    # Convert to ticks
                    new_lower_tick = self.truncate_tick(self.price_to_tick(new_lower_price))
                    new_upper_tick = self.truncate_tick(self.price_to_tick(new_upper_price))
                    
                    if new_lower_tick >= new_upper_tick:
                        new_upper_tick = new_lower_tick + self.tick_spacing
                    
                    print(f"🚀 MPPI Rebalance Proposed:", flush=True)
                    print(f"   Target Price: [{new_lower_price:.2f}, {new_upper_price:.2f}]", flush=True)
                    print(f"   Target Ticks: [{new_lower_tick}, {new_upper_tick}]", flush=True)
                    
                    # Execute on-chain
                    self.execute_rebalance(new_lower_tick, new_upper_tick)
                
                time.sleep(10) # Slower cycle for rebalancing (reduced frequency to lower costs)
                
            except Exception as e:
                print(f"MPPI Bot Error: {e}", flush=True)
                time.sleep(10)

if __name__ == "__main__":
    bot = MPPIBot()
    bot.run()
