import os
import time
import random
import threading
import math
from web3 import Web3
from dotenv import load_dotenv
from data_store import store
from eth_abi import encode
from config import CONTRACTS

load_dotenv()

class SwapBot:
    def __init__(self):
        # RPC priority: Base Sepolia > generic RPC_URL > Anvil local
        self.rpc_url = (
            os.getenv("BASE_SEPOLIA_RPC_URL")
            or os.getenv("RPC_URL")
            or os.getenv("ANVIL_RPC_URL")
            or "http://127.0.0.1:8545"
        )
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        
        # Use a dedicated key for arbitrage to allow "infinite balance" minting locally
        # without accidentally affecting MPPI position sizing.
        # Fallback to BOT_PRIVATE_KEY for backward compatibility.
        self.private_key = os.getenv("BOT_PRIVATE_KEY","0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d")
        
        self.account = self.w3.eth.account.from_key(self.private_key)
        # Initialization logs removed - only log errors

        # Simple portfolio tracking (token1-valued) for "total profit-ish" visibility
        self._initial_portfolio_value_token1 = None
        self._last_portfolio_value_token1 = None

        # Contract addresses from config
        self.pool_manager_address = Web3.to_checksum_address(CONTRACTS['pool_manager'])
        self.permit2_address = Web3.to_checksum_address(
            CONTRACTS.get('permit2', "0x000000000022D473030F116dDEE9F6B43aC78BA3")
        )
        self.swap_router_address = Web3.to_checksum_address(CONTRACTS['swap_router'])
        self.token0_address = Web3.to_checksum_address(CONTRACTS['token0'])
        self.token1_address = Web3.to_checksum_address(CONTRACTS['token1'])
        self.hook_address = Web3.to_checksum_address(CONTRACTS['hook'])
        
        self.fee = 3000
        self.tick_spacing = 60
        
        # PoolManager setup for reading pool state
        self.pool_manager_abi = [
            {
                "inputs": [{"internalType": "bytes32", "name": "slot", "type": "bytes32"}],
                "name": "extsload",
                "outputs": [{"internalType": "bytes32", "name": "value", "type": "bytes32"}],
                "stateMutability": "view",
                "type": "function"
            }
        ]
        
        self.pool_manager = self.w3.eth.contract(
            address=self.pool_manager_address,
            abi=self.pool_manager_abi
        )
        
        self.router_abi = [
            {
                "inputs": [
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
                    {"internalType": "bool", "name": "zeroForOne", "type": "bool"},
                    {
                        "components": [
                            {"internalType": "address", "name": "currency0", "type": "address"},
                            {"internalType": "address", "name": "currency1", "type": "address"},
                            {"internalType": "uint24", "name": "fee", "type": "uint24"},
                            {"internalType": "int24", "name": "tickSpacing", "type": "int24"},
                            {"internalType": "address", "name": "hooks", "type": "address"}
                        ],
                        "internalType": "struct PoolKey",
                        "name": "poolKey",
                        "type": "tuple"
                    },
                    {"internalType": "bytes", "name": "hookData", "type": "bytes"},
                    {"internalType": "address", "name": "receiver", "type": "address"},
                    {"internalType": "uint256", "name": "deadline", "type": "uint256"}
                ],
                "name": "swapExactTokensForTokens",
                "outputs": [{"internalType": "tuple", "name": "delta", "type": "tuple", "components": [
                    {"internalType": "int128", "name": "amount0", "type": "int128"},
                    {"internalType": "int128", "name": "amount1", "type": "int128"}
                ]}],
                "stateMutability": "payable",
                "type": "function"
            }
        ]
        
        self.router = self.w3.eth.contract(
            address=self.swap_router_address,
            abi=self.router_abi
        )

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
            },
            # Mint is available on the local test tokens deployed by our scripts (MockERC20-style).
            # This enables "infinite balance" for local Anvil simulations.
            {
                "constant": False,
                "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
                "name": "mint",
                "outputs": [],
                "type": "function"
            }
        ]
        
        self.permit2_abi = [
            {
                "inputs": [
                    {"internalType": "address", "name": "token", "type": "address"},
                    {"internalType": "address", "name": "spender", "type": "address"},
                    {"internalType": "uint160", "name": "amount", "type": "uint160"},
                    {"internalType": "uint48", "name": "expiration", "type": "uint48"}
                ],
                "name": "approve",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function"
            },
             {
                "inputs": [
                    {"internalType": "address", "name": "owner", "type": "address"},
                    {"internalType": "address", "name": "token", "type": "address"},
                    {"internalType": "address", "name": "spender", "type": "address"}
                ],
                "name": "allowance",
                "outputs": [
                    {"internalType": "uint160", "name": "amount", "type": "uint160"},
                    {"internalType": "uint48", "name": "expiration", "type": "uint48"},
                    {"internalType": "uint48", "name": "nonce", "type": "uint48"}
                ],
                "stateMutability": "view",
                "type": "function"
            }
        ]

        self.permit2 = self.w3.eth.contract(address=self.permit2_address, abi=self.permit2_abi)
        
        self.approve_tokens()
        self.ensure_infinite_balance()
    
    def get_pool_id(self):
        """Calculate pool ID from pool key"""
        pool_key_tuple = (
            self.token0_address,
            self.token1_address,
            self.fee,
            self.tick_spacing,
            self.hook_address
        )
        pool_key_encoded = encode(
            ['address', 'address', 'uint24', 'int24', 'address'],
            pool_key_tuple
        )
        return Web3.keccak(pool_key_encoded)

    def approve_tokens(self):
        """Approve tokens for Router and Permit2"""
        # Approving tokens silently
        max_uint256 = 2**256 - 1
        max_uint160 = 2**160 - 1
        max_uint48 = 2**48 - 1
        
        spenders = [
            ("SwapRouter", self.swap_router_address),
            ("Permit2", self.permit2_address)
        ]
        
        for token_addr in [self.token0_address, self.token1_address]:
            token = self.w3.eth.contract(address=token_addr, abi=self.erc20_abi)
            
            for spender_name, spender_addr in spenders:
                try:
                    current_allowance = token.functions.allowance(self.account.address, spender_addr).call()
                    if current_allowance < max_uint256 // 2:
                        tx = token.functions.approve(spender_addr, max_uint256).build_transaction({
                            'from': self.account.address,
                            'nonce': self.w3.eth.get_transaction_count(self.account.address),
                            'gas': 200000,
                            'gasPrice': self.w3.eth.gas_price
                        })
                        signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
                        tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                        self.w3.eth.wait_for_transaction_receipt(tx_hash)
                        # Approved silently
                except Exception as e:
                    print(f"Token approval error: {e}", flush=True)

            try:
                p2_allowance = self.permit2.functions.allowance(self.account.address, token_addr, self.swap_router_address).call()
                
                if p2_allowance[0] < max_uint160 // 2:
                    tx = self.permit2.functions.approve(
                        token_addr, 
                        self.swap_router_address, 
                        max_uint160, 
                        max_uint48
                    ).build_transaction({
                        'from': self.account.address,
                        'nonce': self.w3.eth.get_transaction_count(self.account.address),
                        'gas': 200000,
                        'gasPrice': self.w3.eth.gas_price
                    })
                    signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
                    tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                    receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
                    if receipt['status'] != 1:
                        print(f"❌ Permit2 Approval failed for {token_addr}", flush=True)
                # Already approved - no log needed

            except Exception as e:
                print(f"Permit2 approval error: {e}", flush=True)

    def ensure_infinite_balance(self):
        """
        Local Anvil helper: keep arb bot balances topped up by calling token.mint().
        This is ONLY intended for local simulations; real networks require funding.
        """
        enabled = os.getenv("ARB_INFINITE_BALANCE", "1").lower() in ("1", "true", "yes", "on")
        if not enabled:
            return

        # Target: 1e9 tokens (in 18 decimals). Refill when below 20%.
        target = int(1_000_000_000 * 1e18)
        refill_threshold = int(target * 0.2)

        for token_addr in [self.token0_address, self.token1_address]:
            try:
                token = self.w3.eth.contract(address=token_addr, abi=self.erc20_abi)
                bal = token.functions.balanceOf(self.account.address).call()
                if bal >= refill_threshold:
                    continue

                amount_to_mint = target - bal
                # Safety clamp (uint256 headroom)
                if amount_to_mint <= 0:
                    continue

                tx = token.functions.mint(self.account.address, int(amount_to_mint)).build_transaction(
                    {
                        "from": self.account.address,
                        "nonce": self.w3.eth.get_transaction_count(self.account.address),
                        "gas": 300_000,
                        "gasPrice": self.w3.eth.gas_price,
                    }
                )
                signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
                if receipt["status"] != 1:
                    print(f"❌ Mint failed for {token_addr}", flush=True)
                else:
                    # Minimal log (once per refill)
                    print(f"🪙 Minted to arb bot: {token_addr} (+{amount_to_mint/1e18:.0f})", flush=True)
            except Exception as e:
                # If mint is not available / restricted, warn once per startup
                print(f"⚠️ Could not mint {token_addr} (arb infinite balance disabled for this token): {e}", flush=True)

    def execute_swap(self, zero_for_one, amount_in):
        """Execute swap"""
        try:
            token_in = self.token0_address if zero_for_one else self.token1_address
            token_contract = self.w3.eth.contract(address=token_in, abi=self.erc20_abi)
            # Keep balances topped up for local simulation
            self.ensure_infinite_balance()
            balance = token_contract.functions.balanceOf(self.account.address).call()
            
            if balance < amount_in:
                # Clamp to available balance (leave a small buffer)
                clamped = int(balance * 0.95)
                if clamped <= 0:
                    print(f"Insufficient balance for swap. Has {balance}, needs {amount_in}", flush=True)
                    return False
                amount_in = clamped
                # If still too small, skip
                if amount_in < int(0.001 * 1e18):
                    print(f"Insufficient balance for swap (too small after clamp). Has {balance}", flush=True)
                    return False

            router_allowance = token_contract.functions.allowance(self.account.address, self.swap_router_address).call()
            max_uint256 = 2**256 - 1
            if router_allowance < max_uint256 // 2:
                # Approve Router silently
                tx = token_contract.functions.approve(self.swap_router_address, max_uint256).build_transaction({
                    'from': self.account.address,
                    'nonce': self.w3.eth.get_transaction_count(self.account.address),
                    'gas': 200000,
                    'gasPrice': self.w3.eth.gas_price
                })
                signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                self.w3.eth.wait_for_transaction_receipt(tx_hash)

            pool_key = (
                self.token0_address,
                self.token1_address,
                self.fee,
                self.tick_spacing,
                self.hook_address
            )
            
            nonce = self.w3.eth.get_transaction_count(self.account.address)
            amount_in_uint256 = int(amount_in) if amount_in <= 2**255 - 1 else amount_in
            
            tx = self.router.functions.swapExactTokensForTokens(
                amount_in_uint256,
                0,
                zero_for_one,
                pool_key,
                b"",
                self.account.address,
                int(time.time()) + 600
            ).build_transaction({
                'from': self.account.address,
                'nonce': nonce,
                'gas': 1000000,
                'gasPrice': self.w3.eth.gas_price
            })
            
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
            
            if receipt['status'] == 1:
                # Fetch price after swap to calculate actual price impact
                try:
                    pool_id = self.get_pool_id()
                    
                    # Fetch slot0 using extsload (same as monitor.py)
                    pools_slot = b'\x00' * 31 + b'\x06'
                    slot = Web3.keccak(pool_id + pools_slot)
                    value = self.pool_manager.functions.extsload(slot).call()
                    data = int.from_bytes(value, byteorder='big')
                    
                    sqrt_price_x96 = data & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
                    tick_raw = (data >> 160) & 0xFFFFFF
                    if tick_raw & (1 << 23):
                        tick_after = tick_raw - (1 << 24)
                    else:
                        tick_after = tick_raw
                    
                    price_after = (sqrt_price_x96 / (2**96)) ** 2

                    # Portfolio value in token1 units (value = token1 + token0 * price)
                    try:
                        bal0 = self.w3.eth.contract(address=self.token0_address, abi=self.erc20_abi).functions.balanceOf(self.account.address).call()
                        bal1 = self.w3.eth.contract(address=self.token1_address, abi=self.erc20_abi).functions.balanceOf(self.account.address).call()
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
                    
                    # Fetch liquidity after swap
                    state_slot_int = int.from_bytes(slot, byteorder='big')
                    liquidity_slot_int = state_slot_int + 3
                    liquidity_slot_bytes = liquidity_slot_int.to_bytes(32, byteorder='big')
                    liquidity_value = self.pool_manager.functions.extsload(liquidity_slot_bytes).call()
                    liquidity_after = int.from_bytes(liquidity_value, byteorder='big')
                    liquidity_after = liquidity_after & ((1 << 128) - 1)
                    
                    print(f"✅ Swap: {'0->1' if zero_for_one else '1->0'} | {amount_in/1e18:.2f} tokens | Price: {price_after:.4f} | Tick: {tick_after} | Liquidity: {liquidity_after/1e18:.2f} | {tx_hash.hex()[:10]}...", flush=True)

                    # Log gas + simple PnL metrics to DB (non-critical)
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
                except Exception as e:
                    print(f"✅ Swap: {'0->1' if zero_for_one else '1->0'} | {amount_in/1e18:.2f} tokens | {tx_hash.hex()[:10]}... (Error fetching post-swap state: {e})", flush=True)
                return True
            else:
                print(f"❌ Swap reverted: {'0->1' if zero_for_one else '1->0'} | Hash: {tx_hash.hex()[:10]}...", flush=True)
                try:
                    result = self.router.functions.swapExactTokensForTokens(
                        amount_in_uint256,
                        0,
                        zero_for_one,
                        pool_key,
                        b"",
                        self.account.address,
                        int(time.time()) + 600
                    ).call({'from': self.account.address, 'gas': 1000000})
                except Exception as call_error:
                    print(f"Revert reason (from call): {call_error}", flush=True)
                return False
            
        except Exception as e:
            print(f"Swap failed: {type(e).__name__}: {e}", flush=True)
            import traceback
            print(f"Traceback: {traceback.format_exc()}", flush=True)
            return False

    def run_noise_trader(self):
        """Noise trader: randomly buy and sell"""
        # Noise Trader starting silently
        while True:
            try:
                time.sleep(random.uniform(5, 15))
                zero_for_one = random.choice([True, False])
                amount = random.uniform(0.1, 1.0) * 1e18
                
                self.execute_swap(zero_for_one, amount)
                
            except Exception as e:
                print(f"Noise trader error: {e}", flush=True)
                time.sleep(5)

    def calculate_optimal_amount(self, pool_price, ext_price, liquidity):
        """
        Calculate swap amount based on price difference
        Adjusted for balanced price tracking with token scaling
        """
        if liquidity == 0:
            return True, 0
        
        # Calculate price difference ratio
        diff_ratio = abs(pool_price - ext_price) / ext_price
        
        # Determine direction: if pool_price > ext_price, sell Token0 (zero_for_one=True)
        zero_for_one = pool_price > ext_price
        
        
        if zero_for_one:
            # Token0 is worth ~2500 Token1
            base_amount = 1.0 * 1e18  # 1.0 Token0 base
            max_amount = 5.0 * 1e18   # Max 5.0 Token0
        else:
            base_amount = 1.0 * 1e18 * pool_price  # Scale by price (~2500e18)
            max_amount = 5.0 * 1e18 * pool_price   # Max 5.0 * Price
            
        amount = int(base_amount * diff_ratio * 10)  # Scale factor 10
        
        # Cap at reasonable limits
        # IMPORTANT: min trade should be in token units (not scaled by price),
        # otherwise 1->0 minimum becomes huge and causes repeated "Insufficient balance".
        min_amount = int(0.01 * 1e18)
             
        amount = max(min_amount, min(amount, int(max_amount)))
        
        return zero_for_one, amount


    def run_arbitrage_bot(self):
        """Arbitrage bot: close price deviation"""
        # Arbitrage Bot starting silently
        while True:
            try:
                time.sleep(3)
                
                import sqlite3
                from data_store import store
                
                with sqlite3.connect(store.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT pool_price, external_price, pool_liquidity, price_lower, price_upper FROM price_history ORDER BY timestamp DESC LIMIT 1")
                    row = cursor.fetchone()
                
                if not row:
                    continue
                    
                pool_price, ext_price, liquidity_raw, price_lower, price_upper = row
                # Convert back from normalized value (was divided by 1e18 in data_store)
                if liquidity_raw is None:
                    liquidity = 0
                else:
                    liquidity = int(float(liquidity_raw) * 1e18)  # Convert back to wei units
                
                if liquidity == 0:
                    continue
                
                # Check if current price is within active liquidity range
                if price_lower and price_upper:
                    if pool_price < price_lower or pool_price > price_upper:
                        # Price is outside active liquidity range, skip arbitrage
                        continue
                
                diff_ratio = (pool_price - ext_price) / ext_price
                THRESHOLD = 0.005  # 0.5% threshold
                
                # Check both directions: pool_price > ext_price and ext_price > pool_price
                if abs(diff_ratio) > THRESHOLD:
                    # Calculate optimal amount (simplified approach)
                    zero_for_one, optimal_amount = self.calculate_optimal_amount(
                        pool_price, ext_price, liquidity
                    )
                    
                    if optimal_amount > 0:
                        # Log concise arb info
                        direction = "0->1" if zero_for_one else "1->0"
                        print(f"⚖️ Arb: Target={ext_price:.2f} | {direction} | Amount={optimal_amount/1e18:.4f}", flush=True)
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
