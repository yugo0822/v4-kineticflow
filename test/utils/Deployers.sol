// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.26;

import {MockERC20} from "solmate/src/test/utils/mocks/MockERC20.sol";

import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";

import {IPermit2} from "permit2/src/interfaces/IPermit2.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolManager} from "@uniswap/v4-core/src/PoolManager.sol";
import {IPositionManager} from "@uniswap/v4-periphery/src/interfaces/IPositionManager.sol";
import {PositionManager} from "@uniswap/v4-periphery/src/PositionManager.sol";
import {IPositionDescriptor} from "@uniswap/v4-periphery/src/interfaces/IPositionDescriptor.sol";
import {IWETH9} from "@uniswap/v4-periphery/src/interfaces/external/IWETH9.sol";

import {IUniswapV4Router04} from "hookmate/interfaces/router/IUniswapV4Router04.sol";
import {AddressConstants} from "hookmate/constants/AddressConstants.sol";

import {Permit2Deployer} from "hookmate/artifacts/Permit2.sol";
import {V4PoolManagerDeployer} from "hookmate/artifacts/V4PoolManager.sol";
import {V4PositionManagerDeployer} from "hookmate/artifacts/V4PositionManager.sol";
import {V4RouterDeployer} from "hookmate/artifacts/V4Router.sol";

/**
 * Base Deployer Contract for Hook Testing
 *
 * Automatically does the following:
 * 1. Setup deployments for Permit2, PoolManager, PositionManager and V4SwapRouter.
 * 2. Check if chainId is 31337, is so, deploys local instances.
 * 3. If not, uses existing canonical deployments on the selected network.
 * 4. Provides utility functions to deploy tokens and currency pairs.
 *
 * This contract can be used for both local testing and fork testing.
 */
abstract contract Deployers {
    IPermit2 permit2;
    IPoolManager poolManager;
    IPositionManager positionManager;
    IUniswapV4Router04 swapRouter;

    function deployToken() internal returns (MockERC20 token) {
        token = new MockERC20("Test Token", "TEST", 18);
        token.mint(address(this), 10_000_000 ether);

        token.approve(address(permit2), type(uint256).max);
        token.approve(address(positionManager), type(uint256).max);
        token.approve(address(swapRouter), type(uint256).max);

        permit2.approve(address(token), address(positionManager), type(uint160).max, type(uint48).max);
        permit2.approve(address(token), address(swapRouter), type(uint160).max, type(uint48).max);
    }

    function deployCurrencyPair() internal virtual returns (Currency currency0, Currency currency1) {
        MockERC20 token0 = deployToken();
        MockERC20 token1 = deployToken();

        if (token0 > token1) {
            (token0, token1) = (token1, token0);
        }

        currency0 = Currency.wrap(address(token0));
        currency1 = Currency.wrap(address(token1));
    }

    function deployPermit2() internal {
        // Always deploy a fresh Permit2 for local testing to ensure it exists and has code
        if (block.chainid == 31337) {
            bytes memory bytecode = Permit2Deployer.initcode();
            address p2;
            assembly {
                p2 := create(0, add(bytecode, 0x20), mload(bytecode))
            }
            require(p2 != address(0), "Permit2 deployment failed");
            permit2 = IPermit2(p2);
        } else {
            address permit2Address = AddressConstants.getPermit2Address();
            if (permit2Address.code.length == 0) {
                _etch(permit2Address, Permit2Deployer.deploy().code);
            }
            permit2 = IPermit2(permit2Address);
        }
    }

    function deployPoolManager() internal virtual {
        if (block.chainid == 31337) {
            poolManager = IPoolManager(address(new PoolManager(msg.sender)));
        } else {
            poolManager = IPoolManager(AddressConstants.getPoolManagerAddress(block.chainid));
        }
    }

    function deployPositionManager() internal virtual {
        if (block.chainid == 31337) {
            // Deploy WETH mock
            MockERC20 weth = new MockERC20("WETH", "WETH", 18);

            positionManager = IPositionManager(address(new PositionManager(poolManager, permit2, 300_000, IPositionDescriptor(address(0)), IWETH9(address(weth)))));
        } else {
            positionManager = IPositionManager(AddressConstants.getPositionManagerAddress(block.chainid));
        }
    }

    function deployRouter() internal virtual {
        if (block.chainid == 31337) {
            // Use deployCode to deploy V4Router using the artifact wrapper logic
            bytes memory args = abi.encode(address(poolManager), address(permit2));
            bytes memory bytecode = abi.encodePacked(V4RouterDeployer.initcode(), args);
            
            // We need to use assembly to deploy since we have bytecode
            address router;
            assembly {
                router := create(0, add(bytecode, 0x20), mload(bytecode))
            }
            require(router != address(0), "Router deployment failed");
            
            swapRouter = IUniswapV4Router04(payable(router));
        } else {
            swapRouter = IUniswapV4Router04(payable(AddressConstants.getV4SwapRouterAddress(block.chainid)));
        }
    }

    function _etch(address, bytes memory) internal virtual {
        revert("Not implemented");
    }

    function deployArtifacts() internal {
        // Order matters.
        deployPermit2();
        deployPoolManager();
        deployPositionManager();
        deployRouter();
    }
}
