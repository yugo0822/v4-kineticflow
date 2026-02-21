import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from streamlit_autorefresh import st_autorefresh
import time
import sqlite3
import os
import requests
from dotenv import load_dotenv
from data_store import store

def _short_address(addr: str, max_chars: int = 10) -> str:
    return f"{addr[:max_chars+2]}...{addr[-4:]}" if len(addr) > (max_chars + 6) else addr


load_dotenv()

st.set_page_config(page_title="Uniswap v4 Monitor", layout="wide")
st_autorefresh(interval=1000, key="data_refresh")

# st.title("📊Dynamic Range Optimizer")
# st.markdown("v4 Dynamic Liquidity Management Dashboard")

@st.cache_data(ttl=2)
def fetch_live_external_price():
    """Fetch real-time price from MockV3Aggregator"""
    try:
        from web3 import Web3
        from config import CONTRACTS
        
        # RPC priority: Base Sepolia > generic RPC_URL > Anvil local
        rpc_url = (
            os.getenv("BASE_SEPOLIA_RPC_URL")
            or os.getenv("RPC_URL")
            or os.getenv("ANVIL_RPC_URL")
            or "http://127.0.0.1:8545"
        )
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        
        oracle_address = CONTRACTS.get('oracle')
        if not oracle_address:
            return None
        
        oracle_abi = [
            {
                "inputs": [],
                "name": "latestRoundData",
                "outputs": [
                    {"internalType": "uint80", "name": "roundId", "type": "uint80"},
                    {"internalType": "int256", "name": "answer", "type": "int256"},
                    {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
                    {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
                    {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"}
                ],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [],
                "name": "decimals",
                "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
                "stateMutability": "view",
                "type": "function"
            }
        ]
        
        oracle = w3.eth.contract(
            address=w3.to_checksum_address(oracle_address),
            abi=oracle_abi
        )
        
        round_id, answer, started_at, updated_at, answered_in_round = oracle.functions.latestRoundData().call()
        decimals = oracle.functions.decimals().call()
        price = float(answer) / (10 ** decimals)
        
        return price
    except Exception as e:
        return None

def load_data(limit=20):
    with sqlite3.connect(store.db_path) as conn:
        query = f"SELECT * FROM price_history ORDER BY timestamp DESC LIMIT {limit}"
        df = pd.read_sql_query(query, conn)
    if 'diff_ratio' not in df.columns and 'diff' in df.columns and 'external_price' in df.columns:
        df['diff_ratio'] = (df['diff'] / df['external_price'] * 100).fillna(0)
    return df.sort_values("timestamp")

df = load_data(limit=20)

if df.empty:
    st.warning("No data available. Please run `python dashboard/monitor.py`.")
    st.stop()

last_row = df.iloc[-1]
last_pool_price = last_row['pool_price']
last_ext_price = last_row['external_price']

use_live_price = os.getenv("USE_LIVE_PRICE_IN_UI", "true").lower() == "true"
live_external_price = None

if use_live_price:
    live_external_price = fetch_live_external_price()
    if live_external_price is not None:
        display_ext_price = live_external_price
        price_source = "🟢 Mock Oracle"
    else:
        display_ext_price = last_ext_price
        price_source = "📊 Database"
else:
    display_ext_price = last_ext_price
    price_source = "📊 Database"

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Pool Price", f"${last_pool_price:.4f}")
with col2:
    st.metric("External Price", f"${display_ext_price:.4f}", help=f"Source: {price_source}")
with col3:
    price_gap = last_pool_price - display_ext_price
    st.metric("Price Gap", f"${price_gap:.4f}")
with col4:
    if 'diff_ratio' in last_row:
        diff_ratio_pct = last_row['diff_ratio'] * 100
    else:
        diff_ratio_pct = (price_gap / display_ext_price * 100) if display_ext_price > 0 else 0
    
    st.metric(
        "Price Deviation", 
        f"{diff_ratio_pct:.2f}%",
    )

# Display liquidity range information
if 'tick_lower' in last_row and 'tick_upper' in last_row and pd.notna(last_row['tick_lower']) and pd.notna(last_row['tick_upper']):
    tick_lower = int(last_row['tick_lower'])
    tick_upper = int(last_row['tick_upper'])
    price_lower = last_row.get('price_lower', 0)
    price_upper = last_row.get('price_upper', 0)
    current_tick = int(last_row.get('tick', 0))
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Lower Tick", f"{tick_lower}", help=f"Price: ${price_lower:.4f}" if price_lower > 0 else "")
    with col2:
        st.metric("Current Tick", f"{current_tick}")
    with col3:
        st.metric("Upper Tick", f"{tick_upper}", help=f"Price: ${price_upper:.4f}" if price_upper > 0 else "")
    
    # Check if current price is in range
    if price_lower > 0 and price_upper > 0:
        in_range = price_lower <= last_pool_price <= price_upper
        range_status = "✓ In Range" if in_range else "⚠️ Out of Range"
        st.info(f"Liquidity Range: [${price_lower:.4f}, ${price_upper:.4f}] | {range_status}")

st.subheader("Real-time Price History")

fig = go.Figure()

fig.add_trace(go.Scatter(
    x=pd.to_datetime(df['timestamp'], unit='s'), 
    y=df['pool_price'],
    mode='lines+markers', name='Pool Price', line=dict(color='cyan', width=2)
))

fig.add_trace(go.Scatter(
    x=pd.to_datetime(df['timestamp'], unit='s'), 
    y=df['external_price'],
    mode='lines', name='External Price (Historical)', line=dict(color='lime', width=2, dash='dot')
))

# Add liquidity range lines if available
if 'price_lower' in df.columns and 'price_upper' in df.columns:
    latest_price_lower = df['price_lower'].iloc[-1] if pd.notna(df['price_lower'].iloc[-1]) else None
    latest_price_upper = df['price_upper'].iloc[-1] if pd.notna(df['price_upper'].iloc[-1]) else None
    
    if latest_price_lower and latest_price_upper:
        latest_timestamp = df['timestamp'].max()
        fig.add_trace(go.Scatter(
            x=[pd.to_datetime(df['timestamp'].min(), unit='s'), pd.to_datetime(df['timestamp'].max(), unit='s')],
            y=[latest_price_lower, latest_price_lower],
            mode='lines',
            name='Lower Tick Price',
            line=dict(color='red', width=2, dash='dash'),
            opacity=0.5
        ))
        fig.add_trace(go.Scatter(
            x=[pd.to_datetime(df['timestamp'].min(), unit='s'), pd.to_datetime(df['timestamp'].max(), unit='s')],
            y=[latest_price_upper, latest_price_upper],
            mode='lines',
            name='Upper Tick Price',
            line=dict(color='red', width=2, dash='dash'),
            opacity=0.5
        ))

if use_live_price and live_external_price is not None:
    latest_timestamp = df['timestamp'].max()
    fig.add_trace(go.Scatter(
        x=[pd.to_datetime(latest_timestamp, unit='s'), pd.to_datetime(time.time(), unit='s')],
        y=[last_ext_price, live_external_price],
        mode='lines+markers',
        name='External Price (Live)',
        line=dict(color='lime', width=3),
        marker=dict(size=8, symbol='star')
    ))

if 'diff_ratio' in df.columns and len(df) > 0:
    fig.add_trace(go.Scatter(
        x=pd.to_datetime(df['timestamp'], unit='s'),
        y=df['diff_ratio'] * 100,
        mode='lines',
        name='Price Deviation (%)',
        line=dict(color='yellow', width=1, dash='dash'),
        yaxis='y2'
    ))
    
    fig.update_layout(
        yaxis2=dict(
            title="Price Deviation (%)",
            overlaying="y",
            side="right",
            range=[-20, 20]
        )
    )

fig.update_layout(template="plotly_dark", height=500, margin=dict(l=20, r=20, t=20, b=20),
                  xaxis_title="Time", yaxis_title="Price (USD)",
                  yaxis=dict(autorange=False, range=[1000, 4000]))

st.plotly_chart(fig, width='stretch')

with st.expander("Raw Data"):
    st.dataframe(df.sort_values("timestamp", ascending=False))

# Contract addresses
st.subheader("Contract addresses")
try:
    from config import CONTRACTS
    labels = {
        "pool_manager": "Pool Manager",
        "position_manager": "Position Manager",
        "permit2": "Permit2",
        "swap_router": "Swap Router",
        "hook": "Hook",
        "token0": "Token0",
        "token1": "Token1",
        "oracle": "Oracle",
    }
    if CONTRACTS and isinstance(CONTRACTS, dict):
        for key, label in labels.items():
            addr = CONTRACTS.get(key)
            if addr:
                st.text(f"{label}: {_short_address(addr)}")
        with st.expander("Raw addresses (copy)"):
            for key, label in labels.items():
                addr = CONTRACTS.get(key)
                if addr:
                    st.code(addr, language=None)
    else:
        st.caption("No addresses file found. Deploy with `make deploy-base-sepolia` and ensure `broadcast/addresses.84532.json` exists. In Docker, the broadcast folder is mounted from the host.")
except Exception as e:
    st.caption(f"Could not load contract addresses: {e}")
