"""
Price Simulator: Fluctuates the price of MockV3Aggregator

Usage:
    python dashboard/price_simulator.py

Environment Variables:
    ANVIL_RPC_URL: RPC endpoint (default: http://127.0.0.1:8545)
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
        self.rpc_url = os.getenv("ANVIL_RPC_URL", "http://127.0.0.1:8545")
        print(f"Price Simulator: Connecting to RPC: {self.rpc_url}", flush=True)
        
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        
        # Check RPC connection
        if not self.w3.is_connected():
            raise ConnectionError(f"Failed to connect to RPC at {self.rpc_url}")
        print(f"Price Simulator: Connected to chain ID: {self.w3.eth.chain_id}", flush=True)
        
        # Private key (use Anvil's default account 0 directly)
        default_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        env_key = os.getenv("PRIVATE_KEY", "")
        
        # Use default if environment variable is empty or invalid
        if env_key and env_key.startswith("0x") and len(env_key) == 66:
            self.private_key = env_key
            print(f"Price Simulator: Using PRIVATE_KEY from env", flush=True)
        else:
            self.private_key = default_key
            print(f"Price Simulator: Using default Anvil account 0", flush=True)
        
        try:
            self.account = self.w3.eth.account.from_key(self.private_key)
            print(f"Price Simulator: Using account: {self.account.address}", flush=True)
        except Exception as e:
            print(f"Price Simulator: Error parsing private key: {e}", flush=True)
            print(f"Price Simulator: Falling back to default key", flush=True)
            self.private_key = default_key
            self.account = self.w3.eth.account.from_key(self.private_key)
            print(f"Price Simulator: Using account: {self.account.address}", flush=True)
        
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
        print(f"Price Simulator: Oracle address from config: {self.oracle_address}", flush=True)
        print(f"Price Simulator: All CONTRACTS: {CONTRACTS}", flush=True)
        
        if not self.oracle_address or self.oracle_address == "0x0000000000000000000000000000000000000000":
            raise ValueError("Oracle address not found in CONTRACTS. Deploy MockV3Aggregator first.")
        
        self.oracle = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.oracle_address),
            abi=self.aggregator_abi
        )
        print("Waiting for contracts to be ready...", flush=True)
        time.sleep(5)
        # Get decimals
        self.decimals = self.oracle.functions.decimals().call()
        
        # Check owner
        try:
            owner = self.oracle.functions.owner().call()
            print(f"Price Simulator: Oracle owner: {owner}", flush=True)
            print(f"Price Simulator: Simulator account: {self.account.address}", flush=True)
            
            if owner.lower() != self.account.address.lower():
                print(f"WARNING: Oracle owner ({owner}) != Simulator account ({self.account.address})", flush=True)
                print("Price updates will fail. Ensure PRIVATE_KEY matches Oracle owner.", flush=True)
            else:
                print("Price Simulator: Owner check PASSED - can update prices", flush=True)
        except Exception as e:
            print(f"Price Simulator: Error checking owner: {e}", flush=True)
    
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
                
                # Create transaction
                tx = self.oracle.functions.updateAnswer(scaled_price).build_transaction({
                    'from': self.account.address,
                    'nonce': nonce,
                    'gas': 300000,
                    'gasPrice': int(self.w3.eth.gas_price * 2)
                })
                
                # Sign and send
                signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                
                # Wait for confirmation
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
                
                return receipt.status == 1
                
            except Exception as e:
                # 1. First, output detailed error information (debugging purpose)
                import traceback
                error_details = traceback.format_exc()
                print(f"DEBUG: Price update failed at attempt {attempt + 1}")
                print(f"ERROR_LOG: {error_details}", flush=True)

                error_str = str(e).lower()
                if 'nonce' in error_str or 'replacement' in error_str:
                    # For nonce errors, wait a bit before returning to the start of the loop
                    time.sleep(0.5) 
                    continue

                # 2. For other errors, also rest a bit before retry
                if attempt == max_retries - 1:
                    return False
                time.sleep(0.5)
        
        return False
    
    def run_scenario(self, scenario: str = "volatile", base_price: float = 1.0, interval: float = 3.0):
        """
        Execute price scenario
        
        Scenarios:
        - volatile: Extreme fluctuations (±5% random walk)
        - crash: Crash simulation (gradually -30%)
        - pump: Pump simulation (gradually +30%)
        - sine: Sine wave (periodic fluctuations)
        - random_walk: Random walk (±2%)
        """
        print("=" * 60, flush=True)
        print(f"Price Simulator starting...", flush=True)
        print(f"Oracle: {self.oracle_address}", flush=True)
        print(f"Account: {self.account.address}", flush=True)
        print(f"Scenario: {scenario}", flush=True)
        print(f"Base price: ${base_price:.2f}", flush=True)
        print(f"Update interval: {interval}s", flush=True)
        print("=" * 60, flush=True)
        
        current_price = base_price
        step = 0
        
        while True:
            try:              
                # Calculate new price based on scenario
                if scenario == "volatile":
                    # Extreme fluctuations: ±5% random walk + occasional spikes
                    change = random.gauss(0, 0.03)  # 3% standard deviation
                    if random.random() < 0.1:  # 10% chance of spike
                        change += random.choice([-0.05, 0.05])  # ±5% spike
                    current_price = current_price * (1 + change)
                    
                elif scenario == "crash":
                    # Crash: gradual decline
                    change = -0.01 + random.gauss(0, 0.005)  # -1% + noise
                    current_price = current_price * (1 + change)
                    # Set minimum price (down to -50%)
                    current_price = max(current_price, base_price * 0.5)
                    
                elif scenario == "pump":
                    # Pump: gradual rise
                    change = 0.01 + random.gauss(0, 0.005)  # +1% + noise
                    current_price = current_price * (1 + change)
                    # Set maximum price (up to +50%)
                    current_price = min(current_price, base_price * 1.5)
                    
                elif scenario == "sine":
                    # Sine wave: periodic fluctuations
                    amplitude = 0.1  # ±10%
                    period = 60  # 1 cycle in 60 steps
                    current_price = base_price * (1 + amplitude * math.sin(2 * math.pi * step / period))
                    
                elif scenario == "random_walk":
                    # Random walk: ±2%
                    change = random.gauss(0, 0.02)
                    current_price = current_price * (1 + change)
                    
                elif scenario == "extreme":
                    # Extreme fluctuations: ±10% random walk + frequent spikes
                    change = random.gauss(0, 0.05)  # 5% standard deviation
                    if random.random() < 0.2:  # 20% chance of spike
                        change += random.choice([-0.1, 0.1])  # ±10% spike
                    current_price = current_price * (1 + change)
                    
                else:
                    print(f"Unknown scenario: {scenario}. Using random_walk.", flush=True)
                    change = random.gauss(0, 0.02)
                    current_price = current_price * (1 + change)
                
                # Ensure price doesn't go below 0
                current_price = max(current_price, 1.0)
                
                # Update price
                success = self.update_price(current_price)
                
                if success:
                    price_change = ((current_price / base_price) - 1) * 100
                    print(f"[{step:04d}] Price: ${current_price:.2f} ({price_change:+.2f}% from base)", flush=True)
                else:
                    print(f"[{step:04d}] Failed to update price", flush=True)
                
                step += 1
                time.sleep(interval)
                
            except KeyboardInterrupt:
                print("\nPrice Simulator stopped.", flush=True)
                break
            except Exception as e:
                print(f"Error in simulation loop: {e}", flush=True)
                time.sleep(interval)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Price Simulator for MockV3Aggregator")
    parser.add_argument("--scenario", type=str, default="volatile",
                        choices=["volatile", "crash", "pump", "sine", "random_walk", "extreme"],
                        help="Price scenario to run")
    parser.add_argument("--base-price", type=float, default=2500.0,
                        help="Base price in USD")
    parser.add_argument("--interval", type=float, default=3.0,
                        help="Update interval in seconds")
    
    args = parser.parse_args()
    
    
    simulator = PriceSimulator()
    simulator.run_scenario(
        scenario=args.scenario,
        base_price=args.base_price,
        interval=args.interval
    )
