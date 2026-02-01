// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @title MockV3Aggregator
/// @notice Chainlink AggregatorV3Interface互換のモックオラクル
/// @dev 価格を自由に設定でき、デモやテストに最適
contract MockV3Aggregator {
    uint8 public decimals;
    int256 public latestAnswer;
    uint256 public latestTimestamp;
    uint256 public latestRound;
    
    // オーナー（価格更新権限）
    address public owner;
    
    // 履歴データ（getRoundData用）
    mapping(uint256 => int256) public getAnswer;
    mapping(uint256 => uint256) public getTimestamp;
    mapping(uint256 => uint256) private getStartedAt;
    
    event AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    
    error OnlyOwner();
    
    modifier onlyOwner() {
        if (msg.sender != owner) revert OnlyOwner();
        _;
    }
    
    constructor(uint8 _decimals, int256 _initialAnswer) {
        decimals = _decimals;
        owner = msg.sender;
        updateAnswer(_initialAnswer);
    }
    
    /// @notice 価格を更新（オーナーのみ）
    /// @param _answer 新しい価格（decimalsでスケール済み）
    function updateAnswer(int256 _answer) public onlyOwner {
        latestAnswer = _answer;
        latestTimestamp = block.timestamp;
        latestRound++;
        getAnswer[latestRound] = _answer;
        getTimestamp[latestRound] = block.timestamp;
        getStartedAt[latestRound] = block.timestamp;
        
        emit AnswerUpdated(_answer, latestRound, block.timestamp);
    }
    
    /// @notice 価格を一括更新（複数回の更新をシミュレート）
    /// @param _answers 価格の配列
    function updateAnswerBatch(int256[] calldata _answers) external onlyOwner {
        for (uint256 i = 0; i < _answers.length; i++) {
            updateAnswer(_answers[i]);
        }
    }
    
    /// @notice Chainlink AggregatorV3Interface互換: 最新のラウンドデータを取得
    function latestRoundData()
        external
        view
        returns (
            uint80 roundId,
            int256 answer,
            uint256 startedAt,
            uint256 updatedAt,
            uint80 answeredInRound
        )
    {
        return (
            uint80(latestRound),
            latestAnswer,
            getStartedAt[latestRound],
            latestTimestamp,
            uint80(latestRound)
        );
    }
    
    /// @notice Chainlink AggregatorV3Interface互換: 指定ラウンドのデータを取得
    function getRoundData(uint80 _roundId)
        external
        view
        returns (
            uint80 roundId,
            int256 answer,
            uint256 startedAt,
            uint256 updatedAt,
            uint80 answeredInRound
        )
    {
        return (
            _roundId,
            getAnswer[_roundId],
            getStartedAt[_roundId],
            getTimestamp[_roundId],
            _roundId
        );
    }
    
    /// @notice Chainlink AggregatorV3Interface互換: 説明を取得
    function description() external pure returns (string memory) {
        return "MockV3Aggregator - ETH / USD";
    }
    
    /// @notice Chainlink AggregatorV3Interface互換: バージョンを取得
    function version() external pure returns (uint256) {
        return 4;
    }
    
    /// @notice オーナーシップを移転
    function transferOwnership(address newOwner) external onlyOwner {
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }
}
