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
        self.rpc_url = os.getenv("ANVIL_RPC_URL", "http://127.0.0.1:8545")
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        
        self.private_key = os.getenv(
            "BOT_PRIVATE_KEY",
            "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
        )
        self.account = self.w3.eth.account.from_key(self.private_key)
        print(f"Bot using account: {self.account.address}", flush=True)
        
        self.pool_manager_address = Web3.to_checksum_address(CONTRACTS['pool_manager'])
        self.permit2_address = Web3.to_checksum_address(CONTRACTS.get('permit2', "0x000000000022D473030F116dDEE9F6B43aC78BA3"))
        self.swap_router_address = Web3.to_checksum_address(CONTRACTS['swap_router'])
        self.token0_address = Web3.to_checksum_address(CONTRACTS['token0'])
        self.token1_address = Web3.to_checksum_address(CONTRACTS['token1'])
        self.hook_address = Web3.to_checksum_address(CONTRACTS['hook'])
        
        print(f"Bot initialized with: PM={self.pool_manager_address}, Router={self.swap_router_address}, Permit2={self.permit2_address}", flush=True)
        
        self.fee = 3000
        self.tick_spacing = 60
        
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

    def approve_tokens(self):
        """Approve tokens for Router and Permit2"""
        print("Approving tokens...", flush=True)
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
                        print(f"Approved {token_addr} for {spender_name} on Token", flush=True)
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
                    if receipt['status'] == 1:
                        print(f"Approved {token_addr} for Router on Permit2", flush=True)
                    else:
                         print(f"Permit2 Approval failed for {token_addr}", flush=True)
                else:
                    print(f"Already approved {token_addr} for Router on Permit2", flush=True)

            except Exception as e:
                print(f"Permit2 approval error: {e}", flush=True)

    def execute_swap(self, zero_for_one, amount_in):
        """Execute swap"""
        try:
            token_in = self.token0_address if zero_for_one else self.token1_address
            token_contract = self.w3.eth.contract(address=token_in, abi=self.erc20_abi)
            balance = token_contract.functions.balanceOf(self.account.address).call()
            
            if balance < amount_in:
                print(f"Insufficient balance for swap. Has {balance}, needs {amount_in}", flush=True)
                return False

            router_allowance = token_contract.functions.allowance(self.account.address, self.swap_router_address).call()
            max_uint256 = 2**256 - 1
            if router_allowance < max_uint256 // 2:
                print(f"Approving Router directly for {token_in}...", flush=True)
                tx = token_contract.functions.approve(self.swap_router_address, max_uint256).build_transaction({
                    'from': self.account.address,
                    'nonce': self.w3.eth.get_transaction_count(self.account.address),
                    'gas': 200000,
                    'gasPrice': self.w3.eth.gas_price
                })
                signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                self.w3.eth.wait_for_transaction_receipt(tx_hash)
                print(f"Router approved for {token_in}", flush=True)

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
                print(f"Swap executed: {'0->1' if zero_for_one else '1->0'} | Amount: {amount_in/1e18:.2f} | Hash: {tx_hash.hex()[:10]}...", flush=True)
                return True
            else:
                print(f"Swap reverted: {'0->1' if zero_for_one else '1->0'} | Hash: {tx_hash.hex()} | Gas used: {receipt.get('gasUsed', 'N/A')}", flush=True)
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
        print("Starting Noise Trader...", flush=True)
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
        """Arbitrage bot: close price deviation"""
        print("Starting Arbitrage Bot...", flush=True)
        while True:
            try:
                time.sleep(3)
                
                import sqlite3
                from data_store import store
                
                with sqlite3.connect(store.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT pool_price, external_price FROM price_history ORDER BY timestamp DESC LIMIT 1")
                    row = cursor.fetchone()
                
                if not row:
                    continue
                    
                pool_price, ext_price = row
                diff_ratio = (pool_price - ext_price) / ext_price
                THRESHOLD = 0.005
                ARB_AMOUNT = 2.0 * 1e18 
                
                if diff_ratio > THRESHOLD:
                    print(f"Arb opp: Pool({pool_price:.2f}) > Ext({ext_price:.2f}). Selling Token0.", flush=True)
                    self.execute_swap(True, ARB_AMOUNT)
                    
                elif diff_ratio < -THRESHOLD:
                    print(f"Arb opp: Pool({pool_price:.2f}) < Ext({ext_price:.2f}). Buying Token0.", flush=True)
                    self.execute_swap(False, ARB_AMOUNT)
                    
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
