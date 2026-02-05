import os
import time
import random
from web3 import Web3
from dotenv import load_dotenv
from config import CONTRACTS

load_dotenv()

class MarketMonitor:
    def __init__(self):
        # RPC priority: Base Sepolia > generic RPC_URL > Anvil local
        self.rpc_url = (
            os.getenv("BASE_SEPOLIA_RPC_URL")
            or os.getenv("RPC_URL")
            or os.getenv("ANVIL_RPC_URL")
            or "http://127.0.0.1:8545"
        )
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        
        self.pool_manager_address = CONTRACTS['pool_manager']
        
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
        
        # PositionManager setup for getting position info
        self.position_manager_address = CONTRACTS.get('position_manager')
        if self.position_manager_address:
            self.position_manager_abi = [
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
                                {"internalType": "address", "name": "hooks", "type": "address"}
                            ],
                            "internalType": "struct PoolKey",
                            "name": "poolKey",
                            "type": "tuple"
                        },
                        {"internalType": "uint256", "name": "info", "type": "uint256"}
                    ],
                    "stateMutability": "view",
                    "type": "function"
                },
                {
                    "inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}],
                    "name": "getPositionLiquidity",
                    "outputs": [{"internalType": "uint128", "name": "", "type": "uint128"}],
                    "stateMutability": "view",
                    "type": "function"
                },
                {
                    "inputs": [],
                    "name": "nextTokenId",
                    "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
                    "stateMutability": "view",
                    "type": "function"
                }
            ]
            self.position_manager = self.w3.eth.contract(
                address=self.w3.to_checksum_address(self.position_manager_address),
                abi=self.position_manager_abi
            )
        else:
            self.position_manager = None
        
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
    
    def fetch_position_ticks(self, pool_id_bytes=None):
        """Fetch tick range from PositionManager for the most recent active position"""
        if not self.position_manager:
            return None, None
        
        try:
            # Get nextTokenId to find the latest token ID
            next_token_id = self.position_manager.functions.nextTokenId().call()
            
            # Try to find an active position by checking from the latest token ID backwards
            # This ensures we get the most recent position after rebalancing
            max_tokens_to_check = min(10, next_token_id)  # Check up to 10 most recent tokens
            
            for i in range(max_tokens_to_check):
                token_id = next_token_id - 1 - i
                if token_id < 1:
                    break
                
                try:
                    pool_key, position_info = self.position_manager.functions.getPoolAndPositionInfo(token_id).call()
                    
                    # Check if this position has active liquidity
                    liquidity = self.position_manager.functions.getPositionLiquidity(token_id).call()
                    if liquidity == 0:
                        continue  # Skip positions with no liquidity
                    
                    # If pool_id_bytes is provided, verify it matches
                    if pool_id_bytes:
                        # Calculate poolId from poolKey
                        # PoolId = keccak256(abi.encode(poolKey))
                        from eth_abi import encode
                        # PoolKey is a struct: (address currency0, address currency1, uint24 fee, int24 tickSpacing, address hooks)
                        pool_key_tuple = (
                            pool_key[0],  # currency0
                            pool_key[1],  # currency1
                            pool_key[2],  # fee
                            pool_key[3],  # tickSpacing
                            pool_key[4]   # hooks
                        )
                        pool_key_encoded = encode(
                            ['address', 'address', 'uint24', 'int24', 'address'],
                            pool_key_tuple
                        )
                        pool_id_calculated = Web3.keccak(pool_key_encoded)
                        
                        # Convert pool_id_bytes to bytes32 if it's a string
                        if isinstance(pool_id_bytes, str):
                            if pool_id_bytes.startswith('0x'):
                                pool_id_bytes = bytes.fromhex(pool_id_bytes[2:])
                            else:
                                pool_id_bytes = bytes.fromhex(pool_id_bytes)
                        
                        if pool_id_calculated != pool_id_bytes:
                            continue  # Skip positions from different pools
                    
                    # PositionInfo layout: 200 bits poolId | 24 bits tickUpper | 24 bits tickLower | 8 bits hasSubscriber
                    # tickLower is at offset 8 bits, tickUpper is at offset 32 bits
                    position_info_int = int(position_info)
                    
                    # Extract tickLower (24 bits at offset 8)
                    tick_lower_raw = (position_info_int >> 8) & 0xFFFFFF
                    # Sign extend int24 (if bit 23 is set, it's negative)
                    if tick_lower_raw & (1 << 23):
                        tick_lower = tick_lower_raw - (1 << 24)
                    else:
                        tick_lower = tick_lower_raw
                    
                    # Extract tickUpper (24 bits at offset 32)
                    tick_upper_raw = (position_info_int >> 32) & 0xFFFFFF
                    # Sign extend int24
                    if tick_upper_raw & (1 << 23):
                        tick_upper = tick_upper_raw - (1 << 24)
                    else:
                        tick_upper = tick_upper_raw
                    
                    return tick_lower, tick_upper
                    
                except Exception:
                    # Token might not exist or be invalid, continue to next
                    continue
            
            # No active position found
            return None, None
            
        except Exception as e:
            print(f"Error fetching position ticks: {e}", flush=True)
            return None, None

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
        print(f"Monitor: Started for pool {pool_id.hex()[:16]}...", flush=True)
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
                # Try to fetch actual position ticks from PositionManager
                # Pass pool_id to ensure we get ticks for the correct pool
                # pool_id is bytes32, convert to bytes if it's a hex string
                pool_id_for_check = pool_id
                if isinstance(pool_id, str):
                    if pool_id.startswith('0x'):
                        pool_id_for_check = bytes.fromhex(pool_id[2:])
                    else:
                        pool_id_for_check = bytes.fromhex(pool_id)
                elif not isinstance(pool_id, bytes):
                    pool_id_for_check = None  # Skip pool_id check if format is unknown
                
                fetched_tick_lower, fetched_tick_upper = self.fetch_position_ticks(pool_id_bytes=pool_id_for_check)
                
                if fetched_tick_lower is not None and fetched_tick_upper is not None:
                    tick_lower = fetched_tick_lower
                    tick_upper = fetched_tick_upper
                else:
                    # Fallback to fixed values from deployment
                    # Wider range: ±2000 ticks ≈ ±20% price range
                    target_tick = 78240
                    tick_lower = target_tick - 2000
                    tick_upper = target_tick + 2000
                
                # If current tick is outside liquidity range, clamp price to range boundary
                if onchain['tick'] < tick_lower:
                    # Price is below lower tick - use lower tick price
                    price_lower = self.tick_to_price(tick_lower)
                    actual_price_from_tick = self.tick_to_price(onchain['tick'])
                    if onchain['price'] < price_lower * 0.99:  # Allow small tolerance
                        onchain['price'] = price_lower
                elif onchain['tick'] > tick_upper:
                    # Price is above upper tick - use upper tick price
                    price_upper = self.tick_to_price(tick_upper)
                    actual_price_from_tick = self.tick_to_price(onchain['tick'])
                    if onchain['price'] > price_upper * 1.01:  # Allow small tolerance
                        onchain['price'] = price_upper
                
                if last_valid_price is None:
                    last_valid_price = onchain['price']
                    last_valid_tick = onchain['tick']
                pool_liquidity = self.fetch_pool_liquidity(pool_id)
                
                # Calculate prices from ticks
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
                
                # Only log when there's a significant change or error condition
                should_log = (
                    abs(diff_ratio) > rebalance_threshold or  # Price deviation is significant
                    not in_range or  # Price is out of range
                    (last_valid_price is not None and abs(onchain['price'] - last_valid_price) / last_valid_price > 0.01)  # Price changed by >1%
                )
                
                if should_log:
                    # Show actual tick and price before clamping
                    actual_tick = onchain.get('tick', 'N/A')
                    actual_price_before_clamp = onchain.get('price', 0)
                    if onchain.get('tick') is not None:
                        actual_price_from_tick = self.tick_to_price(onchain['tick'])
                    else:
                        actual_price_from_tick = 0
                    
                    print(f"[{time.strftime('%H:%M:%S')}] Pool: {data['pool_price']:.4f} | Ext: {data['external_price']:.4f} | Diff: {diff_ratio*100:.2f}% | {status}", flush=True)
                    if not in_range:
                        print(f"  ⚠️ Out of Range: [{price_lower:.2f}, {price_upper:.2f}] | Tick: {actual_tick} (range: {tick_lower}-{tick_upper}) | Actual price from tick: {actual_price_from_tick:.4f}", flush=True)
                
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
    # Monitor starting silently
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
        # MarketMonitor initialized silently
        
        monitor.run_monitoring(POOL_ID)
        
    except KeyboardInterrupt:
        print("Monitoring stopped by user.", flush=True)
    except Exception as e:
        print(f"CRITICAL ERROR in monitor: {type(e).__name__}: {e}", flush=True)
        print(f"Traceback:\n{traceback.format_exc()}", flush=True)
        import time
        time.sleep(5)
        raise
