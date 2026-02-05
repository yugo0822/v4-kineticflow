"""
Price Simulator: Fluctuates the price of MockV3Aggregator

Usage:
    python dashboard/price_simulator.py

Environment Variables:
    BASE_SEPOLIA_RPC_URL or RPC_URL or ANVIL_RPC_URL:
        RPC endpoint (priority order; default: http://127.0.0.1:8545)
    PRIVATE_KEY: Private key for price updates (default: Anvil account 0)
    PRICE_SIMULATOR_INTERVAL: Update interval in seconds (default: 3)
    PRICE_VOLATILITY: Volatility (default: 0.02 = 2%)
"""

import os
import time
import random
import math
from web3 import Web3
from dotenv import load_dotenv
from config import CONTRACTS

load_dotenv()


class PriceSimulator:
    def __init__(self):
        # RPC selection priority: Base Sepolia > generic RPC_URL > Anvil local
        self.rpc_url = (
            os.getenv("BASE_SEPOLIA_RPC_URL")
            or os.getenv("RPC_URL")
            or os.getenv("ANVIL_RPC_URL")
            or "http://127.0.0.1:8545"
        )
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        
        # Check RPC connection
        if not self.w3.is_connected():
            raise ConnectionError(f"Failed to connect to RPC at {self.rpc_url}")
        
        # Private key (use Anvil's default account 0 directly)
        default_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        env_key = os.getenv("PRIVATE_KEY", "")
        
        # Use default if environment variable is empty or invalid
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
        
        # MockV3Aggregator ABI
        self.aggregator_abi = [
            {
                "inputs": [{"internalType": "int256", "name": "_answer", "type": "int256"}],
                "name": "updateAnswer",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function"
            },
            {
                "inputs": [],
                "name": "latestRoundData",
                "outputs": [
                    {"internalType": "uint80", "name": "roundId", "type": "uint80"},
                    {"internalType": "int256", "name": "answer", "type": "int256"},
                    {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
                    {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
                    {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"}
                ],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [],
                "name": "decimals",
                "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [],
                "name": "owner",
                "outputs": [{"internalType": "address", "name": "", "type": "address"}],
                "stateMutability": "view",
                "type": "function"
            }
        ]
        
        # Get oracle address
        self.oracle_address = CONTRACTS.get('oracle')
        if not self.oracle_address:
            raise ValueError("Oracle address not found in CONTRACTS.")

        self.oracle = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.oracle_address),
            abi=self.aggregator_abi
        )

        # Get decimals
        self.decimals = self.oracle.functions.decimals().call()
        
        # Check owner
        try:
            owner = self.oracle.functions.owner().call()
            if owner.lower() != self.account.address.lower():
                print(f"⚠️ Price Simulator: Oracle owner mismatch. Updates may fail.", flush=True)
        except Exception as e:
            print(f"❌ Price Simulator: Error checking owner: {e}", flush=True)

        # =======================
        # Volatility model parameters (GARCH + Jump-Diffusion)
        # Adjusted for smoother, lower frequency price movement
        self.base_vol = 0.005  # Reduced from 0.02 (0.5% base volatility)
        self.current_vol = self.base_vol
        self.vol_persistence = 0.98  # Increased persistence for smoother vol changes
        self.last_return = 0.0
        self.momentum = 0.0
        
        # Jump parameters
        self.jump_intensity = 0.005  # Reduced jump probability (0.5% per step)
        self.jump_size_mean = 0.0
        self.jump_size_std = 0.02  # Reduced jump size (2%)
    
    def get_current_price(self) -> float:
        """Get current price (human-readable format)"""
        _, answer, _, _, _ = self.oracle.functions.latestRoundData().call()
        return float(answer) / (10 ** self.decimals)
    
    def update_price(self, new_price: float) -> bool:
        """Update price (with retry)"""
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                # Scale price
                scaled_price = int(new_price * (10 ** self.decimals))
                
                # Get latest nonce (including pending)
                nonce = self.w3.eth.get_transaction_count(self.account.address, 'pending')
                
                # Check balance before creating transaction
                balance = self.w3.eth.get_balance(self.account.address)
                gas_price = self.w3.eth.gas_price
                estimated_gas_cost = 300000 * gas_price
                
                if balance < estimated_gas_cost:
                    print(f"⚠️ Insufficient balance: {balance/1e18:.6f} ETH < {estimated_gas_cost/1e18:.6f} ETH (gas cost)", flush=True)
                    return False
                
                # Use normal gas price (not 2x) for Anvil
                # Anvil uses very low gas prices, so 2x is unnecessary and might cause issues
                tx = self.oracle.functions.updateAnswer(scaled_price).build_transaction({
                    'from': self.account.address,
                    'nonce': nonce,
                    'gas': 300000,
                    'gasPrice': gas_price  # Use normal gas price, not 2x
                })
                
                # Sign and send
                signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                
                # Wait for confirmation
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
                
                return receipt.status == 1
                
            except Exception as e:
                error_str = str(e).lower()
                
                # Check for insufficient funds error
                if 'insufficient funds' in error_str:
                    # Check ETH balance and gas price
                    try:
                        balance = self.w3.eth.get_balance(self.account.address)
                        balance_eth = balance / 1e18
                        gas_price = self.w3.eth.gas_price
                        gas_price_gwei = gas_price / 1e9
                        estimated_cost = (300000 * gas_price) / 1e18
                        
                        print(f"⚠️ Insufficient funds error. Balance: {balance_eth:.6f} ETH, Gas price: {gas_price_gwei:.2f} gwei, Estimated cost: {estimated_cost:.6f} ETH", flush=True)
                        
                        if balance_eth < 0.01:  # Less than 0.01 ETH
                            print(f"⚠️ CRITICAL: Low ETH balance ({balance_eth:.6f} ETH). Price updates paused.", flush=True)
                            print(f"   Please restart Anvil to refill accounts, or send ETH to: {self.account.address}", flush=True)
                            # Wait longer before retrying to avoid spam
                            time.sleep(10)
                            return False
                        elif estimated_cost > balance_eth:
                            print(f"⚠️ Gas cost ({estimated_cost:.6f} ETH) exceeds balance ({balance_eth:.6f} ETH).", flush=True)
                            return False
                    except Exception as balance_error:
                        print(f"⚠️ Error checking balance: {balance_error}", flush=True)
                
                # 1. First, output detailed error information (only for non-funds errors)
                if 'insufficient funds' not in error_str:
                    import traceback
                    error_details = traceback.format_exc()
                    print(f"DEBUG: Price update failed at attempt {attempt + 1}")
                    print(f"ERROR_LOG: {error_details}", flush=True)

                if 'nonce' in error_str or 'replacement' in error_str:
                    # For nonce errors, wait a bit before returning to the start of the loop
                    time.sleep(0.5) 
                    continue

                # 2. For other errors, also rest a bit before retry
                if attempt == max_retries - 1:
                    return False
                time.sleep(0.5)
        
        return False

    def _generate_market_return(self, scenario, step, base_price, current_price):
        """
        GARCH + Jump-Diffusion モデルに基づく次ステップの収益率計算
        """
        # 1. ボラティリティ・クラスタリング (GARCH-like update)
        # 前回の変動幅が大きいと、次回のボラティリティも上がる
        shock = abs(self.last_return)
        self.current_vol = (self.vol_persistence * self.current_vol + 
                           (1 - self.vol_persistence) * self.base_vol + 
                           0.1 * shock)
        
        # ボラティリティが死なないように（かつ爆発しないように）制限
        self.current_vol = max(self.base_vol * 0.3, min(self.base_vol * 10, self.current_vol))

        # 2. シナリオ別ロジック
        change = 0.0
        
        if scenario in ["volatile", "extreme"]:
            # 拡散項 (Normal diffusion)
            mult = 2.5 if scenario == "extreme" else 1.0
            change = random.gauss(0, self.current_vol * mult)
            
            # 跳躍項 (Jump: 突発的な大きなニュースやクジラの売り買い)
            jump_chance = 0.15 if scenario == "extreme" else 0.05
            if random.random() < jump_chance:
                jump = random.gauss(0, self.base_vol * 5)
                change += jump
                if abs(jump) > 0.03:  # Only log significant jumps (>3%)
                    print(f"   >>> MARKET JUMP: {jump:+.2%}", flush=True)

        elif scenario == "crash":
            # 徐々に下がるトレンド + 恐怖によるボラティリティ増大
            drift = -0.005
            change = drift + random.gauss(0, self.current_vol * 1.5)

        elif scenario == "pump":
            # 強気相場
            drift = 0.005
            change = drift + random.gauss(0, self.current_vol * 1.5)

        elif scenario == "sine":
            # 周期的な動き（ただしノイズ多め）
            target = base_price * (1 + 0.15 * math.sin(2 * math.pi * step / 100))
            change = (target / current_price - 1) + random.gauss(0, self.base_vol * 0.5)

        else: # random_walk
            change = random.gauss(0, self.base_vol)

        # 3. モメンタム (トレンドの継続性)
        change = change + (self.last_return * self.momentum)
        
        # キャッシュして返す
        self.last_return = change
        return change
    
    def run_scenario(self, scenario: str = "volatile", base_price: float = 2500.0, interval: float = 3.0):
        """
        Execute price scenario
        
        Scenarios:
        - volatile: Extreme fluctuations (±5% random walk)
        - crash: Crash simulation (gradually -30%)
        - pump: Pump simulation (gradually +30%)
        - sine: Sine wave (periodic fluctuations)
        - random_walk: Random walk (±2%)
        """
        print(f"Price Simulator: Started | Scenario: {scenario} | Interval: {interval}s", flush=True)
        
        current_price = base_price
        step = 0
        
        while True:
            try:
                ret = self._generate_market_return(scenario, step, base_price, current_price)
                current_price *= (1 + ret)
                current_price = max(current_price, base_price * 0.1) # 0にはならない
                
                success = self.update_price(current_price)
                
                # Only log on significant price changes (>5%) or failures
                if not success:
                    print(f"[{step:04d}] ❌ Failed to update price: ${current_price:,.2f}", flush=True)
                else:
                    diff = ((current_price / base_price) - 1) * 100
                    if abs(diff) > 5.0 or step % 20 == 0:  # Log if >5% change or every 20 steps
                        print(f"[{step:04d}] Price: ${current_price:,.2f} ({diff:+.2f}%) | σ: {self.current_vol:.2%}", flush=True)
                
                step += 1
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\nSimulator stopped.")
                break
            except Exception as e:
                print(f"Loop error: {e}")
                time.sleep(interval)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Price Simulator for MockV3Aggregator")
    parser.add_argument("--scenario", type=str, default="volatile", choices=["volatile", "crash", "pump", "sine", "random_walk", "extreme"])
    parser.add_argument("--base-price", type=float, default=2500.0)
    parser.add_argument("--interval", type=float, default=3.0)
    
    args = parser.parse_args()
    
    
    simulator = PriceSimulator()
    simulator.run_scenario(
        scenario=args.scenario,
        base_price=args.base_price,
        interval=args.interval
    )
