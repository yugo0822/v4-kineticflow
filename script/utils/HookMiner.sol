// SPDX-License-Identifier: MIT
pragma solidity ^0.8.21;

import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";

/// @title HookMiner
/// @notice Project copy with larger MAX_LOOP for forge script --broadcast (CREATE2_FACTORY).
///        Use this in BaseSepoliaRun when the default 160k iterations don't find a salt.
library HookMiner {
    uint160 constant FLAG_MASK = Hooks.ALL_HOOK_MASK;

    // Larger search space for CREATE2_FACTORY + Counter bytecode on testnets
    uint256 constant MAX_LOOP = 1_048_576;

    function find(address deployer, uint160 flags, bytes memory creationCode, bytes memory constructorArgs)
        internal
        view
        returns (address, bytes32)
    {
        flags = flags & FLAG_MASK;
        bytes memory creationCodeWithArgs = abi.encodePacked(creationCode, constructorArgs);

        address hookAddress;
        for (uint256 salt; salt < MAX_LOOP; salt++) {
            hookAddress = computeAddress(deployer, salt, creationCodeWithArgs);

            if (uint160(hookAddress) & FLAG_MASK == flags && hookAddress.code.length == 0) {
                return (hookAddress, bytes32(salt));
            }
        }
        revert("HookMiner: could not find salt");
    }

    function computeAddress(address deployer, uint256 salt, bytes memory creationCodeWithArgs)
        internal
        pure
        returns (address hookAddress)
    {
        return address(
            uint160(uint256(keccak256(abi.encodePacked(bytes1(0xFF), deployer, salt, keccak256(creationCodeWithArgs)))))
        );
    }
}
