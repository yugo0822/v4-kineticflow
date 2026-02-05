// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {BaseScript} from "./base/BaseScript.sol";
import {LiquidityHelpers} from "./base/LiquidityHelpers.sol";

import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {HookMiner} from "@uniswap/v4-periphery/src/utils/HookMiner.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {LiquidityAmounts} from "@uniswap/v4-core/test/utils/LiquidityAmounts.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";

import {MockERC20} from "solmate/src/test/utils/mocks/MockERC20.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {IPositionManager} from "@uniswap/v4-periphery/src/interfaces/IPositionManager.sol";
import {IPermit2} from "permit2/src/interfaces/IPermit2.sol";

import {Counter} from "../src/Counter.sol";
import {MockV3Aggregator} from "../src/MockV3Aggregator.sol";

import "forge-std/console.sol";

/// @notice Script to run the full flow on Base Sepolia (or any testnet)
/// @dev This script is designed to be safer for repeated runs than AnvilRun:
///      - avoids "bot account mint" shortcuts by default
///      - supports re-running without re-initializing a pool if already initialized
///      - writes addresses to a chain-specific file
contract BaseSepoliaRun is BaseScript, LiquidityHelpers {
    function run() external {
        // Use separate keys for deployment and for the MPPI LP owner.
        // - PRIVATE_KEY: deploys contracts, can be any funded testnet account
        // - MPPI_PRIVATE_KEY: owns the initial LP position and receives burn refunds
        uint256 deployerPk = vm.envUint("PRIVATE_KEY");
        uint256 mppiPk = vm.envOr("MPPI_PRIVATE_KEY", deployerPk);
        address mppiAddr = vm.addr(mppiPk);

        // 0) Deploy Uniswap v4 stack and core components with the deployer key
        vm.startBroadcast(deployerPk);

        // 0) Deploy Uniswap v4 Core & Periphery (for hook testing you typically deploy your own stack)
        deployArtifacts();
        console.log("Deployed PoolManager:", address(poolManager));
        console.log("Deployed PositionManager:", address(positionManager));
        console.log("Deployed SwapRouter:", address(swapRouter));

        // 1) Tokens
        // If TOKEN0/TOKEN1 are provided, use them. Otherwise deploy mock tokens.
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

        if (address(token0) > address(token1)) (token0, token1) = (token1, token0);
        Currency c0 = Currency.wrap(address(token0));
        Currency c1 = Currency.wrap(address(token1));

        console.log("Token0:", address(token0));
        console.log("Token1:", address(token1));

        // Mint only if we deployed the tokens.
        // On testnet you can also provide TOKEN0/TOKEN1 and fund the deployer/MPPI externally.
        if (tokensDeployedByScript) {
            uint256 mintAmount0 = vm.envOr("MINT_TOKEN0_AMOUNT", uint256(10_000e18));
            uint256 mintAmount1 = vm.envOr("MINT_TOKEN1_AMOUNT", uint256(10_000e18));
            // Mint directly to the MPPI LP owner so that subsequent burns
            // and rebalances naturally refund tokens to this address.
            token0.mint(mppiAddr, mintAmount0);
            token1.mint(mppiAddr, mintAmount1);
            console.log("Minted token balances to MPPI LP owner:", mppiAddr);
        } else {
            console.log("Skipping mint for existing tokens. Ensure deployer has balance.");
        }

        // 2) Hook deploy (CREATE2 mined address with correct flags)
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

        // 3) Oracle
        // If ORACLE is provided, use it; otherwise deploy MockV3Aggregator (testnet-only convenience).
        address oracleEnv = vm.envOr("ORACLE", address(0));
        MockV3Aggregator oracle;
        if (oracleEnv != address(0)) {
            oracle = MockV3Aggregator(oracleEnv);
            console.log("Using existing oracle:", oracleEnv);
        } else {
            int256 initialAnswer = int256(vm.envOr("ORACLE_INITIAL_PRICE_E8", uint256(2500 * 1e8)));
            oracle = new MockV3Aggregator(8, initialAnswer);
            console.log("Deployed MockV3Aggregator:", address(oracle));
        }

        // 4) PoolKey + Initialize (idempotent)
        PoolKey memory key = PoolKey({
            currency0: c0,
            currency1: c1,
            fee: uint24(vm.envOr("POOL_FEE", uint256(3000))),
            tickSpacing: int24(int256(vm.envOr("TICK_SPACING", uint256(60)))),
            hooks: IHooks(hook)
        });

        int24 targetTick = int24(int256(vm.envOr("TARGET_TICK", uint256(78240))));
        targetTick = truncateTickSpacing(targetTick, key.tickSpacing);
        uint160 sqrtPriceX96 = TickMath.getSqrtPriceAtTick(targetTick);

        // If the pool is already initialized, Slot0 sqrtPriceX96 will be non-zero.
        (uint160 sqrt0,,,) = StateLibrary.getSlot0(poolManager, key.toId());
        if (sqrt0 == 0) {
            int24 initialTick = poolManager.initialize(key, sqrtPriceX96);
            console.log("Pool Initialized");
            console.log("Initial tick:", initialTick);
        } else {
            console.log("Pool already initialized, skipping initialize()");
        }

        // Switch broadcast to the MPPI LP owner for approvals and initial LP mint.
        vm.stopBroadcast();

        // 5) Approvals & Add Liquidity (from MPPI LP owner)
        vm.startBroadcast(mppiPk);

        // 5.1) Approvals (ERC20 + Permit2) for MPPI owner
        token0.approve(address(permit2), type(uint256).max);
        token1.approve(address(permit2), type(uint256).max);
        token0.approve(address(positionManager), type(uint256).max);
        token1.approve(address(positionManager), type(uint256).max);
        token0.approve(address(swapRouter), type(uint256).max);
        token1.approve(address(swapRouter), type(uint256).max);

        // Permit2 approvals (best-effort)
        try permit2.approve(address(token0), address(positionManager), type(uint160).max, type(uint48).max) {
            console.log("Permit2 approval: token0 -> positionManager");
        } catch {
            console.log("Permit2 approval skipped: token0 -> positionManager");
        }
        try permit2.approve(address(token1), address(positionManager), type(uint160).max, type(uint48).max) {
            console.log("Permit2 approval: token1 -> positionManager");
        } catch {
            console.log("Permit2 approval skipped: token1 -> positionManager");
        }
        try permit2.approve(address(token0), address(swapRouter), type(uint160).max, type(uint48).max) {
            console.log("Permit2 approval: token0 -> swapRouter");
        } catch {
            console.log("Permit2 approval skipped: token0 -> swapRouter");
        }
        try permit2.approve(address(token1), address(swapRouter), type(uint160).max, type(uint48).max) {
            console.log("Permit2 approval: token1 -> swapRouter");
        } catch {
            console.log("Permit2 approval skipped: token1 -> swapRouter");
        }

        // 5.2) Add Liquidity
        int24 tickLower = truncateTickSpacing(targetTick - int24(int256(vm.envOr("RANGE_HALF_WIDTH_TICKS", uint256(2000)))), key.tickSpacing);
        int24 tickUpper = truncateTickSpacing(targetTick + int24(int256(vm.envOr("RANGE_HALF_WIDTH_TICKS", uint256(2000)))), key.tickSpacing);

        uint128 liquidity = uint128(vm.envOr("INITIAL_LIQUIDITY", uint256(1000e18)));

        (uint160 sqrtNow,,,) = StateLibrary.getSlot0(poolManager, key.toId());
        uint256 priceNow = FullMath.mulDiv(sqrtNow, sqrtNow, 2**192);
        console.log("Slot0 sqrtPriceX96:", sqrtNow);
        console.log("Calculated price (token1/token0):", priceNow);

        (uint256 amount0Expected, uint256 amount1Expected) = LiquidityAmounts.getAmountsForLiquidity(
            sqrtNow,
            TickMath.getSqrtPriceAtTick(tickLower),
            TickMath.getSqrtPriceAtTick(tickUpper),
            liquidity
        );

        bytes memory hookData = "";
        (bytes memory actions, bytes[] memory mintParams) = _mintLiquidityParams(
            key,
            tickLower,
            tickUpper,
            liquidity,
            amount0Expected + 1,
            amount1Expected + 1,
            mppiAddr,
            hookData
        );
        positionManager.modifyLiquidities(abi.encode(actions, mintParams), block.timestamp + 3600);
        console.log("Liquidity Added");

        vm.stopBroadcast();

        // 6) Write address config
        // Write a chain-specific file to avoid clobbering local Anvil config.
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

        string memory outPath = string.concat("broadcast/addresses.", vm.toString(block.chainid), ".json");
        vm.writeFile(outPath, json);
        console.log("Wrote addresses to:", outPath);

        // Optional: also write to broadcast/addresses.json for dashboard compatibility
        bool writeLatest = vm.envOr("WRITE_ADDRESSES_LATEST", false);
        if (writeLatest) {
            vm.writeFile("broadcast/addresses.json", json);
            console.log("Also wrote addresses to: broadcast/addresses.json");
        }
    }
}

