#!/bin/bash

# Run Price Simulator in background (fluctuates prices)
python3 dashboard/price_simulator.py --scenario volatile --interval 3 &

# Run Monitor in background (fetches prices from MockV3Aggregator)
python3 dashboard/monitor.py &

# Run Trading Bot in background
python3 dashboard/bot.py &

# Run Streamlit in foreground
streamlit run dashboard/app.py
