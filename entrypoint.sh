#!/bin/bash

# ─────────────────────────────────────────────────────────────────────────────
# External price source selection
#
#   PRICE_SOURCE=real (default)
#       market_monitor fetches live ETH/USD from Binance REST API directly.
#       No oracle update transactions needed.  Works on Base Sepolia & mainnet.
#
#   PRICE_SOURCE=mock
#       price_simulator generates synthetic prices and writes them to the
#       MockV3Aggregator on-chain.  Use for local Anvil testing only.
# ─────────────────────────────────────────────────────────────────────────────

if [ "${PRICE_SOURCE:-real}" = "mock" ]; then
    # Override PRICE_PROVIDER so market_monitor reads from MockV3Aggregator
    export PRICE_PROVIDER=mock
    # [Local Anvil only] Fluctuate mock oracle price with synthetic data
    python3 -m dashboard.simulation.price_simulator --scenario volatile --interval 3 &
fi

# Monitor pool state + external price → write to SQLite
# When PRICE_SOURCE=real, market_monitor fetches Binance price directly.
python3 -m dashboard.monitor.market_monitor &

# [Simulation only] Arbitrage + noise trader (testnet / local Anvil)
python3 -m dashboard.simulation.swap_bot &

# MPPI rebalance bot (core — runs on mainnet too)
python3 -m dashboard.core.mppi_bot &

# Streamlit dashboard (foreground)
streamlit run dashboard/app.py
