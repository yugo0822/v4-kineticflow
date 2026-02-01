import os
import time
import random
from web3 import Web3
from dotenv import load_dotenv
from config import CONTRACTS

load_dotenv()

class MarketMonitor:
    def __init__(self):
        self.rpc_url = os.getenv("ANVIL_RPC_URL", "http://127.0.0.1:8545")
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        
        self.pool_manager_address = CONTRACTS['pool_manager']
        print(f"Monitor: Using PoolManager at {self.pool_manager_address}", flush=True)
        
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
            address=self.w3.to_checksum_address(self.pool_manager_address),
            abi=self.pool_manager_abi
        )
        
        self.history = []

    def sqrt_price_to_human(self, sqrt_price_x96):
        """Convert sqrtPriceX96 to human-readable price"""
        if sqrt_price_x96 == 0:
            return 0
        price = (sqrt_price_x96 / (2**96)) ** 2
        return price
    
    def fetch_pool_liquidity(self, pool_id):
        """Fetch total pool liquidity"""
        try:
            pools_slot = b'\x00' * 31 + b'\x06'
            state_slot = Web3.keccak(pool_id + pools_slot)
            
            state_slot_int = int.from_bytes(state_slot, byteorder='big')
            liquidity_slot_int = state_slot_int + 3
            liquidity_slot_bytes = liquidity_slot_int.to_bytes(32, byteorder='big')
            
            value = self.pool_manager.functions.extsload(liquidity_slot_bytes).call()
            liquidity = int.from_bytes(value, byteorder='big')
            liquidity = liquidity & ((1 << 128) - 1)
            
            return liquidity
        except Exception as e:
            print(f"Error fetching pool liquidity: {e}", flush=True)
            return None
    
    def tick_to_price(self, tick):
        """Convert tick to price"""
        import math
        return 1.0001 ** tick

    def fetch_onchain_price(self, pool_id):
        """Fetch price from Uniswap v4 PoolManager"""
        try:
            pools_slot = b'\x00' * 31 + b'\x06'
            slot = Web3.keccak(pool_id + pools_slot)
            
            value = self.pool_manager.functions.extsload(slot).call()
            data = int.from_bytes(value, byteorder='big')
            
            if data == 0:
                return None

            sqrtPriceX96 = data & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
            
            tick_raw = (data >> 160) & 0xFFFFFF
            if tick_raw & (1 << 23):
                tick = tick_raw - (1 << 24)
            else:
                tick = tick_raw
            
            protocolFee = (data >> 184) & 0xFFFFFF
            lpFee = (data >> 208) & 0xFFFFFF
            
            if sqrtPriceX96 == 0:
                return None
            
            max_uint160 = (1 << 160) - 1
            if sqrtPriceX96 > max_uint160:
                return None
            
            # MIN_TICK = -887272, MAX_TICK = 887272
            # These are valid ticks, but if tick is exactly at these bounds, it might indicate uninitialized pool
            # Only reject if tick is outside the valid range
            if tick < -887272 or tick > 887272:
                return None
            
            price = self.sqrt_price_to_human(sqrtPriceX96)
            import math
            price_from_tick = 1.0001 ** tick
            
            # If price calculation fails or results in invalid value, use tick-based price
            if price > 1e10 or price <= 0:
                # Fallback to tick-based price calculation
                if tick >= -887272 and tick <= 887272:
                    price = price_from_tick
                    if price <= 0 or price > 1e10:
                        return None
                else:
                    return None
                
            return {
                "sqrtPriceX96": sqrtPriceX96,
                "tick": tick,
                "price": price,
                "protocolFee": protocolFee,
                "lpFee": lpFee
            }
        except Exception as e:
            print(f"Error fetching on-chain data: {e}", flush=True)
            return None

    
    def fetch_mock_oracle_price(self):
        """Fetch price from MockV3Aggregator"""
        try:
            oracle_address = CONTRACTS.get('oracle')
            if not oracle_address:
                return None
            
            oracle_abi = [
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
                }
            ]
            
            oracle = self.w3.eth.contract(
                address=self.w3.to_checksum_address(oracle_address),
                abi=oracle_abi
            )
            
            round_id, answer, started_at, updated_at, answered_in_round = oracle.functions.latestRoundData().call()
            decimals = oracle.functions.decimals().call()
            price = float(answer) / (10 ** decimals)
            
            return price
            
        except Exception as e:
            print(f"Error fetching mock oracle price: {e}", flush=True)
            return None
        
    def run_monitoring(self, pool_id, interval=2):
        """Collect data periodically and save to DB"""
        print(f"Start monitoring Pool: {pool_id} on {self.rpc_url}...", flush=True)
        from data_store import store 
        last_valid_price = None
        last_valid_tick = None
        
        while True:
            onchain = self.fetch_onchain_price(pool_id)
            external_price = self.fetch_mock_oracle_price()
            
            if external_price is None:
                current_pool_price = onchain['price'] if onchain and onchain.get('price', 0) > 0 else (last_valid_price or 2500)
                external_price = current_pool_price + random.uniform(-10, 10)
                print("Warning: Using fallback dummy price (oracle unavailable)", flush=True)
            
            if onchain and onchain.get('price', 0) > 0:
                # If tick is at MIN_TICK or MAX_TICK, price might be at liquidity range boundary
                # Calculate expected price at lower/upper tick for comparison
                target_tick = 78240
                tick_lower = target_tick - 600
                tick_upper = target_tick + 600
                
                # If current tick is outside liquidity range, clamp price to range boundary
                if onchain['tick'] < tick_lower:
                    # Price is below lower tick - use lower tick price
                    price_lower = self.tick_to_price(tick_lower)
                    if onchain['price'] < price_lower * 0.99:  # Allow small tolerance
                        onchain['price'] = price_lower
                        print(f"Warning: Price below lower tick, clamping to {price_lower:.4f}", flush=True)
                elif onchain['tick'] > tick_upper:
                    # Price is above upper tick - use upper tick price
                    price_upper = self.tick_to_price(tick_upper)
                    if onchain['price'] > price_upper * 1.01:  # Allow small tolerance
                        onchain['price'] = price_upper
                        print(f"Warning: Price above upper tick, clamping to {price_upper:.4f}", flush=True)
                
                if last_valid_price is None:
                    last_valid_price = onchain['price']
                    last_valid_tick = onchain['tick']
                pool_liquidity = self.fetch_pool_liquidity(pool_id)
                
                target_tick = 78240
                tick_lower = target_tick - 600
                tick_upper = target_tick + 600
                price_lower = self.tick_to_price(tick_lower)
                price_upper = self.tick_to_price(tick_upper)
                
                diff_absolute = onchain['price'] - external_price
                diff_ratio = diff_absolute / external_price if external_price > 0 else 0
                
                data = {
                    "timestamp": time.time(),
                    "pool_price": onchain['price'],
                    "external_price": external_price,
                    "tick": onchain['tick'],
                    "tick_lower": tick_lower,
                    "tick_upper": tick_upper,
                    "price_lower": price_lower,
                    "price_upper": price_upper,
                    "pool_liquidity": pool_liquidity if pool_liquidity else 0,
                    "diff": diff_absolute,
                    "diff_ratio": diff_ratio
                }
                
                store.append_data(data)
                
                rebalance_threshold = 0.005
                if abs(diff_ratio) > rebalance_threshold:
                    status = "⚠️ REBALANCE NEEDED"
                elif abs(diff_ratio) > rebalance_threshold * 0.5:
                    status = "⚡ Monitor"
                else:
                    status = "✓ OK"
                
                in_range = price_lower <= onchain['price'] <= price_upper
                range_status = "✓ In Range" if in_range else "⚠️ Out of Range"
                
                print(f"Time: {time.strftime('%H:%M:%S')} | Pool: {data['pool_price']:.4f} | Ext: {data['external_price']:.4f} | Diff: {diff_ratio*100:.2f}% | {status}", flush=True)
                print(f"  Range: [{price_lower:.2f}, {price_upper:.2f}] | Tick: [{tick_lower}, {tick_upper}] | Current: {onchain['tick']} | {range_status}", flush=True)
                if pool_liquidity:
                    print(f"  Pool Liquidity: {pool_liquidity / 1e18:.2f}", flush=True)
                
                last_valid_price = onchain['price']
                last_valid_tick = onchain['tick']
            else:
                if last_valid_price:
                    print(f"Waiting for pool data... (last valid price: {last_valid_price:.4f}, tick: {last_valid_tick})", flush=True)
                else:
                    print("Waiting for pool data...", flush=True)
            
            time.sleep(interval)

if __name__ == "__main__":
    from web3 import Web3
    from eth_abi import encode
    import traceback
    
    print("=" * 60, flush=True)
    print("Monitor.py starting...", flush=True)
    print(f"CONTRACTS loaded: {CONTRACTS}", flush=True)
    print("=" * 60, flush=True)
    
    try:
        def compute_pool_id(token0, token1, fee, tick_spacing, hooks):
            token0 = Web3.to_checksum_address(token0)
            token1 = Web3.to_checksum_address(token1)
            hooks = Web3.to_checksum_address(hooks)

            if token0.lower() > token1.lower():
                token0, token1 = token1, token0
                
            print(f"Computing PoolID with: Token0={token0}, Token1={token1}, Fee={fee}, TS={tick_spacing}, Hooks={hooks}", flush=True)

            encoded = encode(
                ['address', 'address', 'uint24', 'int24', 'address'],
                [token0, token1, fee, tick_spacing, hooks]
            )
            return Web3.keccak(encoded)

        if 'token0' not in CONTRACTS or 'token1' not in CONTRACTS or 'hook' not in CONTRACTS:
            print(f"ERROR: Missing required contract addresses in CONTRACTS: {CONTRACTS}", flush=True)
            raise ValueError("Missing contract addresses")
        
        POOL_ID = compute_pool_id(
            CONTRACTS['token0'],
            CONTRACTS['token1'],
            3000,
            60,
            CONTRACTS['hook']
        )
        
        print(f"Computed Pool ID: {POOL_ID.hex()}", flush=True)
        
        monitor = MarketMonitor()
        print(f"MarketMonitor initialized. Starting monitoring for Pool ID: {POOL_ID.hex()}", flush=True)
        
        monitor.run_monitoring(POOL_ID)
        
    except KeyboardInterrupt:
        print("Monitoring stopped by user.", flush=True)
    except Exception as e:
        print(f"CRITICAL ERROR in monitor: {type(e).__name__}: {e}", flush=True)
        print(f"Traceback:\n{traceback.format_exc()}", flush=True)
        import time
        time.sleep(5)
        raise
