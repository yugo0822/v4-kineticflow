// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

// ─────────────────────────────────────────────────────────────────────────────
// This script does NOT inherit from Deployers / BaseScript / LiquidityHelpers.
// Those base contracts embed the full v4 artifact bytecodes (Permit2, PoolManager,
// PositionManager, SwapRouter) which inflated the compiled script to ~111 KB and
// caused MemoryOOG when HookMiner looped over large memory.
//
// Instead, canonical Base Sepolia addresses are hardcoded and only the contracts
// we actually deploy (Counter hook, MockERC20 tokens, MockV3Aggregator oracle)
// contribute bytecode to this script.
// ─────────────────────────────────────────────────────────────────────────────

import {Script} from "forge-std/Script.sol";
import "forge-std/console.sol";

import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {LiquidityAmounts} from "@uniswap/v4-core/test/utils/LiquidityAmounts.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";

import {IPositionManager} from "@uniswap/v4-periphery/src/interfaces/IPositionManager.sol";
import {Actions} from "@uniswap/v4-periphery/src/libraries/Actions.sol";

import {IPermit2} from "permit2/src/interfaces/IPermit2.sol";

import {MockERC20} from "solmate/src/test/utils/mocks/MockERC20.sol";

import {HookMiner} from "./utils/HookMiner.sol";

import {Counter} from "../src/Counter.sol";
import {MockV3Aggregator} from "../src/MockV3Aggregator.sol";

/// @notice Deploys the KineticFlow hook + test tokens + oracle on Base Sepolia,
///         then initializes the pool and adds the initial LP position.
///         Uses pre-deployed canonical Uniswap v4 contracts (no re-deployment).
contract BaseSepoliaRun is Script {
    // =========================================================================
    // Canonical Base Sepolia (84532) addresses
    // Source: https://docs.uniswap.org/contracts/v4/deployments
    // =========================================================================
    address constant POOL_MANAGER     = 0x05E73354cFDd6745C338b50BcFDfA3Aa6fA03408;
    address constant POSITION_MANAGER = 0x4B2C77d209D3405F41a037Ec6c77F7F5b8e2ca80;
    address constant PERMIT2          = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
    /// @dev hookmate IUniswapV4Router04 on Base Sepolia (from AddressConstants)
    address constant SWAP_ROUTER      = 0x71cD4Ea054F9Cb3D3BF6251A00673303411A7DD9;
    // CREATE2_FACTORY (0x4e59b44847b379578588920cA78FbF26c0B4956C) is already
    // declared in forge-std/src/Base.sol as `CREATE2_FACTORY` and inherited via Script.

    function run() external {
        // ── Keys ──────────────────────────────────────────────────────────────
        // PRIVATE_KEY  : deploys contracts (any funded testnet account)
        // MPPI_PRIVATE_KEY : owns the LP position and receives burn refunds
        uint256 deployerPk = vm.envUint("PRIVATE_KEY");
        uint256 mppiPk     = vm.envOr("MPPI_PRIVATE_KEY", deployerPk);
        address mppiAddr   = vm.addr(mppiPk);

        IPoolManager     poolManager     = IPoolManager(POOL_MANAGER);
        IPositionManager positionManager = IPositionManager(POSITION_MANAGER);
        IPermit2         permit2         = IPermit2(PERMIT2);

        vm.startBroadcast(deployerPk);

        console.log("Using PoolManager:    ", POOL_MANAGER);
        console.log("Using PositionManager:", POSITION_MANAGER);
        console.log("Using SwapRouter:     ", SWAP_ROUTER);
        console.log("Using Permit2:        ", PERMIT2);

        // ── 1) Tokens ─────────────────────────────────────────────────────────
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

        // Canonical token ordering (lower address = currency0)
        if (address(token0) > address(token1)) (token0, token1) = (token1, token0);
        Currency c0 = Currency.wrap(address(token0));
        Currency c1 = Currency.wrap(address(token1));

        console.log("Token0:", address(token0));
        console.log("Token1:", address(token1));

        if (tokensDeployedByScript) {
            uint256 mintAmount0 = vm.envOr("MINT_TOKEN0_AMOUNT", uint256(10_000e18));
            uint256 mintAmount1 = vm.envOr("MINT_TOKEN1_AMOUNT", uint256(10_000e18));
            token0.mint(mppiAddr, mintAmount0);
            token1.mint(mppiAddr, mintAmount1);
            console.log("Minted token balances to MPPI LP owner:", mppiAddr);
        } else {
            console.log("Skipping mint for existing tokens. Ensure deployer has balance.");
        }

        // ── 2) Hook deploy via CREATE2 ────────────────────────────────────────
        uint160 flags = uint160(
            Hooks.BEFORE_SWAP_FLAG | Hooks.AFTER_SWAP_FLAG
                | Hooks.BEFORE_ADD_LIQUIDITY_FLAG | Hooks.BEFORE_REMOVE_LIQUIDITY_FLAG
        );
        bytes memory constructorArgs = abi.encode(address(poolManager));

        // Tip: pre-mine the salt with the helper below to skip on-chain looping:
        //   forge script script/BaseSepoliaRun.s.sol --sig "mineSalt()" --rpc-url <url>
        // Then set HOOK_SALT=<result> in .env and re-run the full deploy.
        bytes32 salt = bytes32(vm.envOr("HOOK_SALT", bytes32(0)));
        address hookAddress;

        if (salt != bytes32(0)) {
            // Use pre-mined salt directly — avoids the expensive HookMiner loop.
            hookAddress = HookMiner.computeAddress(
                CREATE2_FACTORY,
                uint256(salt),
                abi.encodePacked(type(Counter).creationCode, constructorArgs)
            );
            console.log("Using pre-mined HOOK_SALT:", uint256(salt));
        } else {
            // Mine on-chain (slower; only needed on first deploy or after bytecode change).
            (hookAddress, salt) = HookMiner.find(
                CREATE2_FACTORY, flags, type(Counter).creationCode, constructorArgs
            );
        }

        Counter hook = new Counter{salt: salt}(IPoolManager(POOL_MANAGER));
        require(address(hook) == hookAddress, "Hook address mismatch");
        console.log("Deployed Hook:", address(hook));

        // ── 3) Oracle ─────────────────────────────────────────────────────────
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

        // ── 4) PoolKey + Initialize (idempotent) ──────────────────────────────
        PoolKey memory key = PoolKey({
            currency0:   c0,
            currency1:   c1,
            fee:         uint24(vm.envOr("POOL_FEE",     uint256(3000))),
            tickSpacing: int24(int256(vm.envOr("TICK_SPACING", uint256(60)))),
            hooks:       IHooks(address(hook))
        });

        int24 targetTick = int24(int256(vm.envOr("TARGET_TICK", uint256(78240))));
        targetTick = _truncateTick(targetTick, key.tickSpacing);
        uint160 sqrtPriceX96 = TickMath.getSqrtPriceAtTick(targetTick);

        (uint160 sqrt0,,,) = StateLibrary.getSlot0(poolManager, key.toId());
        if (sqrt0 == 0) {
            int24 initialTick = poolManager.initialize(key, sqrtPriceX96);
            console.log("Pool initialized. Initial tick:", initialTick);
        } else {
            console.log("Pool already initialized, skipping initialize()");
        }

        vm.stopBroadcast();

        // ── 5) Approvals + initial LP position (MPPI LP owner key) ───────────
        vm.startBroadcast(mppiPk);

        token0.approve(address(permit2),         type(uint256).max);
        token1.approve(address(permit2),         type(uint256).max);
        token0.approve(address(positionManager), type(uint256).max);
        token1.approve(address(positionManager), type(uint256).max);
        token0.approve(SWAP_ROUTER,              type(uint256).max);
        token1.approve(SWAP_ROUTER,              type(uint256).max);

        _tryPermit2(permit2, address(token0), address(positionManager), "token0 -> positionManager");
        _tryPermit2(permit2, address(token1), address(positionManager), "token1 -> positionManager");
        _tryPermit2(permit2, address(token0), SWAP_ROUTER,              "token0 -> swapRouter");
        _tryPermit2(permit2, address(token1), SWAP_ROUTER,              "token1 -> swapRouter");

        {
            int24 halfWidth = int24(int256(vm.envOr("RANGE_HALF_WIDTH_TICKS", uint256(2000))));
            int24 tickLower = _truncateTick(targetTick - halfWidth, key.tickSpacing);
            int24 tickUpper = _truncateTick(targetTick + halfWidth, key.tickSpacing);
            uint128 liquidity = uint128(vm.envOr("INITIAL_LIQUIDITY", uint256(1000e18)));

            (uint160 sqrtNow,,,) = StateLibrary.getSlot0(poolManager, key.toId());
            console.log("Slot0 sqrtPriceX96:", sqrtNow);
            console.log("Calculated price (token1/token0):", FullMath.mulDiv(sqrtNow, sqrtNow, 2**192));

            (uint256 amount0Expected, uint256 amount1Expected) = LiquidityAmounts.getAmountsForLiquidity(
                sqrtNow,
                TickMath.getSqrtPriceAtTick(tickLower),
                TickMath.getSqrtPriceAtTick(tickUpper),
                liquidity
            );

            (bytes memory actions, bytes[] memory mintParams) = _mintParams(
                key, tickLower, tickUpper, liquidity,
                amount0Expected + 1, amount1Expected + 1, mppiAddr
            );
            positionManager.modifyLiquidities(abi.encode(actions, mintParams), block.timestamp + 3600);
            console.log("Liquidity added");
        }

        vm.stopBroadcast();

        // ── 6) Write address config ───────────────────────────────────────────
        _writeAddresses(address(token0), address(token1), address(hook), address(oracle));
    }

    // =========================================================================
    // Helper: pre-mine the hook salt without broadcasting.
    // Usage: forge script script/BaseSepoliaRun.s.sol --sig "mineSalt()" --rpc-url <url>
    // Copy the logged salt value and set HOOK_SALT=<value> in .env.
    // =========================================================================
    function mineSalt() external view {
        uint256 deployerPk = vm.envUint("PRIVATE_KEY");
        address poolManager = POOL_MANAGER;
        bytes memory constructorArgs = abi.encode(poolManager);
        uint160 flags = uint160(
            Hooks.BEFORE_SWAP_FLAG | Hooks.AFTER_SWAP_FLAG
                | Hooks.BEFORE_ADD_LIQUIDITY_FLAG | Hooks.BEFORE_REMOVE_LIQUIDITY_FLAG
        );
        (address hookAddress, bytes32 salt) = HookMiner.find(
            CREATE2_FACTORY, flags, type(Counter).creationCode, constructorArgs
        );
        console.log("Found salt (set HOOK_SALT=<value> in .env):");
        console.log("  HOOK_SALT:", uint256(salt));
        console.log("  Hook address:", hookAddress);
        console.log("  Deployer key used:", vm.addr(deployerPk));
    }

    // =========================================================================
    // Internal helpers (inlined from LiquidityHelpers to avoid Deployers import)
    // =========================================================================

    function _truncateTick(int24 tick, int24 spacing) internal pure returns (int24) {
        /// forge-lint: disable-next-line(divide-before-multiply)
        return (tick / spacing) * spacing;
    }

    function _mintParams(
        PoolKey memory poolKey,
        int24 tickLower,
        int24 tickUpper,
        uint256 liquidity,
        uint256 amount0Max,
        uint256 amount1Max,
        address recipient
    ) internal pure returns (bytes memory actions, bytes[] memory params) {
        actions = abi.encodePacked(
            uint8(Actions.MINT_POSITION),
            uint8(Actions.SETTLE_PAIR),
            uint8(Actions.SWEEP),
            uint8(Actions.SWEEP)
        );
        params = new bytes[](4);
        params[0] = abi.encode(poolKey, tickLower, tickUpper, liquidity, amount0Max, amount1Max, recipient, bytes(""));
        params[1] = abi.encode(poolKey.currency0, poolKey.currency1);
        params[2] = abi.encode(poolKey.currency0, recipient);
        params[3] = abi.encode(poolKey.currency1, recipient);
    }

    function _tryPermit2(IPermit2 permit2, address token, address spender, string memory label) internal {
        try permit2.approve(token, spender, type(uint160).max, type(uint48).max) {
            console.log("Permit2 approval:", label);
        } catch {
            console.log("Permit2 approval skipped:", label);
        }
    }

    function _writeAddresses(address token0, address token1, address hook, address oracle) internal {
        string memory json = "{\"pool_manager\": \"";
        json = string.concat(json, vm.toString(POOL_MANAGER));
        json = string.concat(json, "\", \"position_manager\": \"");
        json = string.concat(json, vm.toString(POSITION_MANAGER));
        json = string.concat(json, "\", \"permit2\": \"");
        json = string.concat(json, vm.toString(PERMIT2));
        json = string.concat(json, "\", \"swap_router\": \"");
        json = string.concat(json, vm.toString(SWAP_ROUTER));
        json = string.concat(json, "\", \"token0\": \"");
        json = string.concat(json, vm.toString(token0));
        json = string.concat(json, "\", \"token1\": \"");
        json = string.concat(json, vm.toString(token1));
        json = string.concat(json, "\", \"hook\": \"");
        json = string.concat(json, vm.toString(hook));
        json = string.concat(json, "\", \"oracle\": \"");
        json = string.concat(json, vm.toString(oracle));
        json = string.concat(json, "\"}");

        string memory outPath = string.concat("broadcast/addresses.", vm.toString(block.chainid), ".json");
        vm.writeFile(outPath, json);
        console.log("Wrote addresses to:", outPath);

        if (vm.envOr("WRITE_ADDRESSES_LATEST", false)) {
            vm.writeFile("broadcast/addresses.json", json);
            console.log("Also wrote to: broadcast/addresses.json");
        }
    }
}
