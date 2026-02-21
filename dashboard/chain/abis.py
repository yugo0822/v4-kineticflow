"""
Consolidated ABI definitions for all contracts used across the project.
"""

ERC20_ABI = [
    {
        "constant": False,
        "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    # mint() is available on MockERC20 tokens used in local/testnet simulation.
    # Calling this on a real ERC20 that lacks mint() will simply revert.
    {
        "constant": False,
        "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "name": "mint",
        "outputs": [],
        "type": "function",
    },
]

PERMIT2_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "token", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint160", "name": "amount", "type": "uint160"},
            {"internalType": "uint48", "name": "expiration", "type": "uint48"},
        ],
        "name": "approve",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "owner", "type": "address"},
            {"internalType": "address", "name": "token", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [
            {"internalType": "uint160", "name": "amount", "type": "uint160"},
            {"internalType": "uint48", "name": "expiration", "type": "uint48"},
            {"internalType": "uint48", "name": "nonce", "type": "uint48"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

POOL_MANAGER_ABI = [
    {
        "inputs": [{"internalType": "bytes32", "name": "slot", "type": "bytes32"}],
        "name": "extsload",
        "outputs": [{"internalType": "bytes32", "name": "value", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "bytes32", "name": "poolId", "type": "bytes32"}],
        "name": "getSlot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"internalType": "int24", "name": "tick", "type": "int24"},
            {"internalType": "uint16", "name": "protocolFee", "type": "uint16"},
            {"internalType": "uint16", "name": "lpFee", "type": "uint16"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

POSITION_MANAGER_ABI = [
    {
        "inputs": [
            {"internalType": "bytes", "name": "unlockData", "type": "bytes"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "modifyLiquidities",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "nextTokenId",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
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
                    {"internalType": "address", "name": "hooks", "type": "address"},
                ],
                "internalType": "struct PoolKey",
                "name": "poolKey",
                "type": "tuple",
            },
            {"internalType": "uint256", "name": "info", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}],
        "name": "getPositionLiquidity",
        "outputs": [{"internalType": "uint128", "name": "liquidity", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
]

SWAP_ROUTER_ABI = [
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
                    {"internalType": "address", "name": "hooks", "type": "address"},
                ],
                "internalType": "struct PoolKey",
                "name": "poolKey",
                "type": "tuple",
            },
            {"internalType": "bytes", "name": "hookData", "type": "bytes"},
            {"internalType": "address", "name": "receiver", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForTokens",
        "outputs": [
            {
                "internalType": "tuple",
                "name": "delta",
                "type": "tuple",
                "components": [
                    {"internalType": "int128", "name": "amount0", "type": "int128"},
                    {"internalType": "int128", "name": "amount1", "type": "int128"},
                ],
            }
        ],
        "stateMutability": "payable",
        "type": "function",
    },
]

ORACLE_ABI = [
    {
        "inputs": [{"internalType": "int256", "name": "_answer", "type": "int256"}],
        "name": "updateAnswer",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "owner",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]
