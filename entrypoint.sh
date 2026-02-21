#!/bin/bash

# [Simulation only] Fluctuate oracle price (testnet / local Anvil)
python3 -m dashboard.simulation.price_simulator --scenario volatile --interval 3 &

# Monitor pool state and write to DB
python3 -m dashboard.monitor.market_monitor &

# [Simulation only] Arbitrage + noise trader (testnet / local Anvil)
python3 -m dashboard.simulation.swap_bot &

# MPPI rebalance bot (core - runs on mainnet too)
python3 -m dashboard.core.mppi_bot &

# Streamlit dashboard (foreground)
streamlit run dashboard/app.py
