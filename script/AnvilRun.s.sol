// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {BaseScript} from "./base/BaseScript.sol";
import {LiquidityHelpers} from "./base/LiquidityHelpers.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {HookMiner} from "@uniswap/v4-periphery/src/utils/HookMiner.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {Constants} from "@uniswap/v4-core/test/utils/Constants.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {LiquidityAmounts} from "@uniswap/v4-core/test/utils/LiquidityAmounts.sol";
import {MockERC20} from "solmate/src/test/utils/mocks/MockERC20.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {IPositionManager} from "@uniswap/v4-periphery/src/interfaces/IPositionManager.sol";
import {EasyPosm} from "../test/utils/libraries/EasyPosm.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {IPermit2} from "permit2/src/interfaces/IPermit2.sol";
import "forge-std/console.sol";

import {Counter} from "../src/Counter.sol";
import {MockV3Aggregator} from "../src/MockV3Aggregator.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";

/// @notice Script to run the full flow on a local Anvil chain
contract AnvilRun is BaseScript, LiquidityHelpers {
    using EasyPosm for IPositionManager;

    function run() external {
        vm.startBroadcast();

        // 0. Deploy Uniswap v4 Core & Periphery (if not present)
        // Must be done inside broadcast to ensure real transactions
        deployArtifacts();
        
        console.log("Deployed PoolManager:", address(poolManager));
        console.log("Deployed PositionManager:", address(positionManager));
        console.log("Deployed SwapRouter:", address(swapRouter));

        // 1. Deploy Tokens
        // Check if token addresses are provided in env vars (for Testnet), otherwise deploy Mocks (for Anvil)
        address token0Env = vm.envOr("TOKEN0", address(0));
        address token1Env = vm.envOr("TOKEN1", address(0));
        
        MockERC20 token0;
        MockERC20 token1;
        bool tokensDeployedByScript = false;

        if (token0Env != address(0) && token1Env != address(0)) {
            console.log("Using existing tokens from env vars");
            token0 = MockERC20(token0Env);
            token1 = MockERC20(token1Env);
        } else {
            console.log("Deploying new MockERC20 tokens");
            token0 = new MockERC20("Test Token 0", "TEST0", 18);
            token1 = new MockERC20("Test Token 1", "TEST1", 18);
            tokensDeployedByScript = true;
        }

        if (address(token0) > address(token1)) {
            (token0, token1) = (token1, token0);
        }

        Currency c0 = Currency.wrap(address(token0));
        Currency c1 = Currency.wrap(address(token1));

        console.log("Token0:", address(token0));
        console.log("Token1:", address(token1));

        // Mint tokens to the deployer ONLY if we deployed them
        if (tokensDeployedByScript) {
            token0.mint(msg.sender, 10000e18);
            token1.mint(msg.sender, 10000e18);
            
            // Anvilアカウント1（bot用）にもトークンをmint
            // Address: 0x70997970C51812dc3A010C7d01b50e0d17dc79C8
            address botAccount = 0x70997970C51812dc3A010C7d01b50e0d17dc79C8;
            token0.mint(botAccount, 5000e18);
            token1.mint(botAccount, 5000e18);
            console.log("Minted tokens to bot account:", botAccount);
        } else {
            console.log("Skipping mint for existing tokens. Ensure deployer has balance.");
        }

        // Approve tokens for Permit2, PositionManager and SwapRouter
        token0.approve(address(permit2), type(uint256).max);
        token1.approve(address(permit2), type(uint256).max);
        token0.approve(address(positionManager), type(uint256).max);
        token1.approve(address(positionManager), type(uint256).max);
        token0.approve(address(swapRouter), type(uint256).max);
        token1.approve(address(swapRouter), type(uint256).max);

        // Approve PositionManager and SwapRouter on Permit2
        // Use try-catch to handle potential failures (e.g., if approvals already exist or nonce issues)
        // These failures are non-critical and won't prevent deployment
        try permit2.approve(address(token0), address(positionManager), type(uint160).max, type(uint48).max) {
            console.log("Permit2 approval: token0 -> positionManager");
        } catch {
            console.log("Permit2 approval skipped: token0 -> positionManager (may already exist)");
        }
        try permit2.approve(address(token1), address(positionManager), type(uint160).max, type(uint48).max) {
            console.log("Permit2 approval: token1 -> positionManager");
        } catch {
            console.log("Permit2 approval skipped: token1 -> positionManager (may already exist)");
        }
        try permit2.approve(address(token0), address(swapRouter), type(uint160).max, type(uint48).max) {
            console.log("Permit2 approval: token0 -> swapRouter");
        } catch {
            console.log("Permit2 approval skipped: token0 -> swapRouter (may already exist)");
        }
        try permit2.approve(address(token1), address(swapRouter), type(uint160).max, type(uint48).max) {
            console.log("Permit2 approval: token1 -> swapRouter");
        } catch {
            console.log("Permit2 approval skipped: token1 -> swapRouter (may already exist)");
        }

        // 2. Deploy Hook
        uint160 flags = uint160(
            Hooks.BEFORE_SWAP_FLAG | Hooks.AFTER_SWAP_FLAG | Hooks.BEFORE_ADD_LIQUIDITY_FLAG
                | Hooks.BEFORE_REMOVE_LIQUIDITY_FLAG
        );

        bytes memory constructorArgs = abi.encode(poolManager);
        (address hookAddress, bytes32 salt) =
            HookMiner.find(CREATE2_FACTORY, flags, type(Counter).creationCode, constructorArgs);

        Counter hook = new Counter{salt: salt}(poolManager);
        require(address(hook) == hookAddress, "Hook address mismatch");
        console.log("Deployed Hook:", address(hook));

        // 2.5. Deploy MockV3Aggregator 
        // decimals: 8 
        // initialAnswer: 2500 * 1e8 = $2500.00
        int256 initialAnswer = 2500 * 1e8;
        MockV3Aggregator oracle = new MockV3Aggregator(8, initialAnswer);
        console.log("Deployed MockV3Aggregator:", address(oracle));
        console.log("Oracle initial price: $2500.00");

        // 3. Create Pool
        PoolKey memory key = PoolKey({
            currency0: c0,
            currency1: c1,
            fee: 3000,
            tickSpacing: 60,
            hooks: IHooks(hook)
        });

        // Calculate sqrtPriceX96 for price 2500 (token1/token0 = 2500)
        // price = 2500, sqrt(2500) = 50, sqrtPriceX96 = 50 * 2^96
        // tick ≈ log(2500) / log(1.0001) ≈ 78244
        // Round to nearest tick spacing (60): 78240
        int24 targetTick = 78240;
        uint160 sqrtPriceX96_2500 = TickMath.getSqrtPriceAtTick(targetTick);
        
        int24 initialTick = poolManager.initialize(key, sqrtPriceX96_2500);
        console.log("Pool Initialized with price 2500");
        console.log("Initial sqrtPriceX96:", sqrtPriceX96_2500);
        console.log("Initial tick:", initialTick);
        
        // Verify initial price
        (uint160 sqrtPriceX96_after_init, int24 tick_after_init,,) = StateLibrary.getSlot0(poolManager, key.toId());
        // price = (sqrtPriceX96 / 2^96)^2 = (sqrtPriceX96^2) / 2^192
        uint256 price_after_init = FullMath.mulDiv(sqrtPriceX96_2500, sqrtPriceX96_2500, 2**192);
        console.log("After init - sqrtPriceX96:", sqrtPriceX96_after_init);
        console.log("After init - tick:", tick_after_init);
        console.log("After init - calculated price (token1/token0):", price_after_init);

        // 4. Add Liquidity
        // Approve tokens for PositionManager
        token0.approve(address(positionManager), type(uint256).max);
        token1.approve(address(positionManager), type(uint256).max);

        // int24 tickLower = TickMath.minUsableTick(key.tickSpacing);
        // int24 tickUpper = TickMath.maxUsableTick(key.tickSpacing);

        // Wider range: ±2000 ticks ≈ ±20% price range
        // This prevents frequent out-of-range situations with high volatility
        // Round to tickSpacing to avoid TickMisaligned error
        int24 tickLower = truncateTickSpacing(targetTick - 2000, key.tickSpacing);
        int24 tickUpper = truncateTickSpacing(targetTick + 2000, key.tickSpacing);

        uint128 liquidity = 1000e18; // Increased from 100e18 to 1000e18 for better price stability
        
        (uint256 amount0Expected, uint256 amount1Expected) = LiquidityAmounts.getAmountsForLiquidity(
            sqrtPriceX96_2500,
            TickMath.getSqrtPriceAtTick(tickLower),
            TickMath.getSqrtPriceAtTick(tickUpper),
            liquidity
        );

        positionManager.mint(
            key,
            tickLower,
            tickUpper,
            liquidity,
            amount0Expected + 1, // slippage
            amount1Expected + 1, // slippage
            msg.sender,
            block.timestamp + 60,
            ""
        );
        console.log("Liquidity Added");
        
        // Verify price after adding liquidity
        (uint160 sqrtPriceX96_after_liq, int24 tick_after_liq,,) = StateLibrary.getSlot0(poolManager, key.toId());
        uint256 price_after_liq = FullMath.mulDiv(sqrtPriceX96_after_liq, sqrtPriceX96_after_liq, 2**192);
        console.log("After liquidity - sqrtPriceX96:", sqrtPriceX96_after_liq);
        console.log("After liquidity - tick:", tick_after_liq);
        console.log("After liquidity - calculated price (token1/token0):", price_after_liq);


        vm.stopBroadcast();

        // Write address config to file for dashboard
        string memory json = "{\"pool_manager\": \"";
        json = string.concat(json, vm.toString(address(poolManager)));
        json = string.concat(json, "\", \"position_manager\": \"");
        json = string.concat(json, vm.toString(address(positionManager)));
        json = string.concat(json, "\", \"permit2\": \"");
        json = string.concat(json, vm.toString(address(permit2)));
        json = string.concat(json, "\", \"swap_router\": \"");
        json = string.concat(json, vm.toString(address(swapRouter)));
        json = string.concat(json, "\", \"token0\": \"");
        json = string.concat(json, vm.toString(address(token0)));
        json = string.concat(json, "\", \"token1\": \"");
        json = string.concat(json, vm.toString(address(token1)));
        json = string.concat(json, "\", \"hook\": \"");
        json = string.concat(json, vm.toString(address(hook)));
        json = string.concat(json, "\", \"oracle\": \"");
        json = string.concat(json, vm.toString(address(oracle)));
        json = string.concat(json, "\"}");
        
        vm.writeFile("broadcast/addresses.json", json);
        console.log("Wrote addresses to broadcast/addresses.json");
    }
}
