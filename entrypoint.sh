#!/bin/bash

# Run Price Simulator in background (fluctuates prices)
echo "Starting Price Simulator..."
python3 dashboard/price_simulator.py --scenario volatile --interval 3 &

# Run Monitor in background (fetches prices from MockV3Aggregator)
echo "Starting Monitor..."
python3 dashboard/monitor.py &

# Run Trading Bot in background
echo "Starting Trading Bot..."
python3 dashboard/bot.py &

# Run Streamlit in foreground
echo "Starting Dashboard..."
streamlit run dashboard/app.py
