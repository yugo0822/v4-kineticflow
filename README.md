# v4-KineticFlow: Predictive Liquidity for Uniswap v4

**_From reflexes to foresight: a model-predictive LP agent on Base._**

`v4-KineticFlow` is an agentic liquidity vault for **Uniswap v4** that uses  
**Model Predictive Path Integral (MPPI)** control to _predict_ future prices and  
rebalance concentrated liquidity ranges **before** the market moves there.

Built on **Base Sepolia** with **Uniswap v4 Hooks** and a Python control engine,  
the system continuously:

- simulates **\(10^3+\)** stochastic price paths,
- scores each sequence with a **control-theoretic cost**, and
- executes the optimal range update onchain via `PositionManager.modifyLiquidities`.

This is not just another “reactive” rebalance bot.  
It is a **stochastic optimal control policy** running **inside a DeFi protocol**.

---

## TL;DR

- **Problem**: Today’s LP strategies are mostly _reactive_. They chase price after it moves, leak fees when out-of-range, and burn capital on gas with naive rules.
- **Idea**: Bring **control theory** and **stochastic optimal control** into Uniswap v4.  
  Use **MPPI** to simulate thousands of possible future paths and place liquidity  
  **where the price is most likely to be**, not where it is now.
- **Implementation**:
  - Uniswap v4 stack + Hook on **Base Sepolia**
  - Python MPPI engine controlling **ticks**, not prices
  - Arbitrage bot that anchors pool price to an external oracle
  - Streamlit dashboard + SQLite datastore with PnL & gas metrics
- **Outcome**: A research-grade LP agent that can be benchmarked against naive strategies and extended into a production vault.

---

## The Problem: Reactive Concentrated Liquidity

Uniswap v3/v4 gave LPs **concentrated liquidity**, but left them with a hard problem:

- **Impermanent Loss (IL)** grows when LPs are stuck out-of-range.
- **Capital efficiency** collapses if ranges are too wide “just to be safe”.
- **Most bots are reactive**:
  - “If price moves by X%, shift range by Y%”
  - “Every N blocks, recenter around spot”

These heuristics:

- **ignore the distribution of future prices**,  
- **ignore gas** and fee vs. rebalance trade-offs in a principled way, and
- behave like _PID controllers_ in a world that actually needs **predictive control**.

For an LP vault to be truly competitive, it must:

- anticipate where price _will be_, not just where it _is now_, and  
- optimize **fee income − gas cost − IL** over a horizon, not per-tx.

---

## The Solution: An MPPI Engine on Uniswap v4

### MPPI in One Paragraph

MPPI (Model Predictive Path Integral control) is a sampling-based stochastic optimal control method.  
Instead of solving the Hamilton–Jacobi–Bellman PDE exactly, MPPI:

1. Samples many control sequences \(\{u_{0:T}^k\}_{k=1}^K\),
2. Simulates trajectories \(x_{0:T}^k\) with dynamics \(x_{t+1} = f(x_t, u_t^k, \xi_t^k)\),
3. Computes path costs
   \[
   S^k = \sum_{t=0}^{T-1} \ell(x_t^k, u_t^k) + \phi(x_T^k),
   \]
4. Reweights controls using an exponential weighting
   \[
   u_t^\star = \frac{\sum_k \exp(-S^k / \lambda)\, u_t^k}{\sum_k \exp(-S^k / \lambda)},
   \]
5. Applies only the **first element** \(u_0^\star\) onchain, then repeats (receding horizon).

This gives a **model-predictive** controller that:

- respects system dynamics \(f\),
- can encode rich risk/reward via \(\ell\) and \(\phi\),
- is naturally parallelizable (sample-based), and
- is robust to non-linearities and stochasticity.

### Our State & Control: Working in Tick Space

Uniswap v4 is discrete in **ticks**.  
We therefore formulate the MPPI problem directly in ticks:

- **State** \(x_t \in \mathbb{R}^4\):
  \[
  x_t = [t_\text{mkt}(t),\ t_\text{pool}(t),\ t_\text{center}(t),\ w_\text{ticks}(t)],
  \]
  where:
  - \(t_\text{mkt}\): external market tick (oracle-implied)
  - \(t_\text{pool}\): current pool tick (onchain)
  - \(t_\text{center}\): center tick of current LP range
  - \(w_\text{ticks}\): half-width of LP range in ticks

- **Control** \(u_t \in \mathbb{R}^2\):
  \[
  u_t = [\Delta t_\text{center},\ \Delta w_\text{ticks}],
  \]
  which directly map to target ticks:
  \[
  t_\text{center}^{\text{new}} = t_\text{center} + \Delta t_\text{center},\quad
  w_\text{ticks}^{\text{new}} = w_\text{ticks} + \Delta w_\text{ticks}.
  \]

The corresponding **target Uniswap v4 range** is:
\[
t_\text{lower} = t_\text{center}^{\text{new}} - w_\text{ticks}^{\text{new}},\quad
t_\text{upper} = t_\text{center}^{\text{new}} + w_\text{ticks}^{\text{new}}.
\]

These ticks are truncated to the pool’s tick spacing and then passed to  
`PositionManager.modifyLiquidities` via a carefully encoded `unlockData`.

### Dynamics: Tick-Based Jump-Diffusion

We model market tick dynamics as a **jump-diffusion** on log-price, then map to ticks:

- Let \(p_t\) be the log-price; then
  \[
  p_{t+1} = p_t + \mu \Delta t + \sigma_t \sqrt{\Delta t}\,\epsilon_t + J_t,
  \]
  with:
  - \(\epsilon_t \sim \mathcal{N}(0, 1)\),
  - \(J_t\) a jump term (rare, larger shocks),
  - \(\sigma_t\) following a GARCH-like update to capture volatility clustering.

- Tick is approximately linear in log-price, so
  \[
  t_\text{mkt}(t+1) = t_\text{mkt}(t) + \kappa \Delta p_t + \text{noise}.
  \]

The pool tick \(t_\text{pool}\) is then modeled as:

- tracking \(t_\text{mkt}\) **inside** the LP range, and  
- being **clamped** at \(t_\text{lower}, t_\text{upper}\) when price tries to exit the range.

This gives a **Uniswap-v4-aware** dynamics model `uniswap_dynamics` used by the MPPI sampler.

### Cost Function: Tracking vs. Risk vs. Gas

Our stage cost \(\ell(x_t, u_t)\) is defined directly in ticks:

- **Tracking error**:
  \[
  \ell_\text{track} = w_\text{track} \cdot (t_\text{mkt} - t_\text{center})^2
  \]
  to keep the center of liquidity near the market.

- **Out-of-range penalty**:
  \[
  \ell_\text{oor} = w_\text{oor} \cdot \mathbf{1}\{t_\text{mkt} \notin [t_\text{lower}, t_\text{upper}]\},
  \]
  discouraging states where LP capital is inactive.

- **Boundary / proximity penalty**:
  - extra cost when \(t_\text{mkt}\) is very close to \(t_\text{lower}\) or \(t_\text{upper}\).

- **Control / rebalance cost**:
  \[
  \ell_\text{rebalance} = w_\text{rebalance} \cdot \mathbf{1}\{|\Delta t_\text{center}| + |\Delta w_\text{ticks}| > \tau\},
  \]
  which proxies gas: small jitters are free; big shifts cost.

The **terminal cost** \(\phi(x_T)\) reinforces:

- the final center being close to the market tick, and  
- the market remaining well inside the range.

All weights \((w_\text{track}, w_\text{oor}, w_\text{rebalance}, \dots)\) and thresholds are tuned in **tick units**, making the controller stable across different price levels.

---
## System Architecture

### High-Level Overview

The system bridges off-chain predictive compute with on-chain execution via Uniswap v4 Hooks on Base Sepolia.

```mermaid
graph TD
    subgraph "Off-Chain (Python Control Plane)"
        MPPI[🤖 MPPI Engine<br/>(Stochastic Control)]
        DB[(SQLite DB<br/>State & Metrics)]
        Arb[⚖️ Arb Bot<br/>(Price Driver)]
        Sim[📈 Price Simulator<br/>(Jump-Diffusion)]
    end

    subgraph "On-Chain (Base Sepolia L2)"
        subgraph "Uniswap v4 Protocol"
            PM[PoolManager]
            PosM[PositionManager]
            Pool[v4 Pool<br/>(Token0/Token1)]
        end
        Hook([🪝 Custom v4 Hook<br/>(Instrumentation)])
    end

    %% Data Flow Arrows
    Sim -->|Generates Price| Arb
    Arb -->|Swaps to move price| SwapRouter[SwapRouter] --> PM
    PM -- Updates --> Pool
    Pool -.->|Read State (Ticks)| MPPI
    MPPI -->|Sample & Optimize| MPPI
    MPPI -->|Execute Optimal Range| PosM
    PosM -->|mint/burn| PM
    PM -- Calls --> Hook

    %% Styling
    classDef offchain fill:#f9f,stroke:#333,stroke-width:2px;
    classDef onchain fill:#ccf,stroke:#333,stroke-width:2px,stroke-dasharray: 5 5;
    classDef protocol fill:#eee,stroke:#333,stroke-width:1px;
    class Hook onchain;
    class MPPI,DB,Arb,Sim offchain;
    class PM,PosM,Pool,SwapRouter protocol;
## System Architecture

### High-Level Overview

- **Onchain (Solidity / Base Sepolia)**:
  - `PoolManager`, `PositionManager`, `SwapRouter`, Permit2
  - v4 Hook (`Counter`) for swap/range instrumentation
  - `MockERC20` tokens, `MockV3Aggregator` oracle

- **Offchain agents (Python)**:
  - `MPPIBot` — computes and executes optimal LP ranges:
    - reads pool state + DB state
    - runs MPPI over tick dynamics
    - calls `modifyLiquidities` for burn + mint
  - `SwapBot` — arbitrage agent:
    - reads pool vs oracle price
    - executes swaps via `SwapRouter` to drag pool price toward oracle
  - `PriceSimulator` — drives oracle updates (for controlled experiments)
  - `MarketMonitor` — persists pool/oracle data to SQLite

- **Dashboard (Streamlit)**:
  - connects to `market_data.db`
  - shows price series, deviations, and LP range in real time
  - can be extended to show PnL curves (from `tx_events`)

The Base Sepolia environment is deployed via:

- `script/BaseSepoliaRun.s.sol`  
  which:
  - deploys the v4 stack and tokens,
  - deploys oracle & hook,
  - mints initial LP position **from the MPPI wallet** (so all burn refunds go to MPPI),
  - writes all addresses into `broadcast/addresses.84532.json`.

---

## Demo & Early Results (Base Sepolia)

We run the full stack on **Base Sepolia**:

- **Price Simulator**: generates realistic, jumpy markets via GARCH + jump-diffusion.
- **SwapBot**: continuously arbitrages the pool towards the oracle, with:
  - asymmetric trade sizing,
  - balance clamps,
  - optional “infinite mint” only on local Anvil.
- **MPPIBot**:
  - regularly proposes new ranges
  - executes burn + mint via `modifyLiquidities`
  - logs:
    - successful rebalance Tx hashes,
    - gas usage (burn, mint, total),
    - MPPI wallet’s token1-equivalent portfolio value and PnL.

In live Base Sepolia runs we observe:

- repeated sequences of:
  - `🔥 Burning old position: ...`
  - `🌱 Minting new position: TargetRange=[..., ...], Liquidity=...`
  - `✅ Rebalance successful! Tx: ...`
  - `📊 MPPI PnL: value=..., PnL=..., cumGas=...`
- arbitrage swaps keeping pool price within a few % of the oracle, even across jumps.

The current controller is not yet “fully profit-maximised” — it is a **research-grade baseline** which:

- demonstrates **feasibility** of MPPI-driven LP control on a real chain,
- logs all relevant metrics (PnL, gas, range updates) for **offline evaluation**, and
- provides a clean playground for **benchmarking vs naive rebalancers**.

---

## Roadmap

### Short-Term (Hackathon-Ready)

- **Metrics dashboard**:
  - add PnL & gas plots in `app.py` using `tx_events`
  - support comparisons between:
    - MPPI controller,
    - fixed-range LP,
    - naive periodic recentering.

- **Parameter sweeps**:
  - horizon length, number of samples, noise scales
  - cost weights vs realized fee income and gas

- **Safer production defaults**:
  - stricter min/max liquidity,
  - refined price-impact caps for arb swaps.

### Mid-Term: From MPPI to PDE & Advanced Control

- **PDE-backed approximations**:
  - approximate value functions by solving simplified HJB/PDEs offline,
  - use MPPI only as an online refinement layer.

- **Continuous-time modeling**:
  - richer SDEs for price dynamics,
  - explicit modeling of L2 block timing and MEV-style risks.

- **Multi-pool / multi-asset coupling**:
  - MPPI over a vector of pools (e.g., correlated assets),
  - cross-pool hedging and coordinated range placement.

### Long-Term Vision

The long-term goal is to build a **general-purpose control layer for DeFi**, where:

- LP vaults, liquidators, and routers are all modeled as **interacting control systems**, and
- DeFi can borrow the last 50 years of **control theory, PDEs, and stochastic calculus**  
  instead of reinventing ad-hoc heuristics every cycle.

`v4-KineticFlow` is a first step: a concrete, running MPPI agent on Uniswap v4.

---

## Setup & Usage

### Requirements

- **Foundry** (`forge`) for deploying the v4 stack and hooks.
- **Python 3.10+** with:
  - `torch`, `numpy`, `pandas`, `web3`, `streamlit`, etc. (see `requirements.txt`).
- **Docker** (optional) for running the dashboard as a container.

### Environment

1. Copy `.env.example` to `.env` and fill in:
   - `BASE_SEPOLIA_RPC_URL`
   - `PRIVATE_KEY`, `ARB_PRIVATE_KEY`, `MPPI_PRIVATE_KEY`
2. Ensure `.env` is **never committed**.

### Typical Workflow (Base Sepolia)

1. Deploy:

```bash
make deploy-base-sepolia
```

2. Fund:

- send Base Sepolia ETH to deployer, arb, and MPPI wallets,
- ensure MPPI wallet holds token0/token1 (script does this for mock tokens).

3. Run the stack:

```bash
make build
make run
make logs
```

4. Open the dashboard:

- `http://localhost:8501` in your browser.

5. Inspect:

- price vs oracle,
- LP range over time,
- `📊 Arb PnL` / `📊 MPPI PnL` lines in logs,
- Tx hashes on Base Sepolia explorer for rebalance and swap events.

---

## Team / Author

This project is built by a **Kyoto University** student specializing in:

- **control engineering** (modeling, stochastic systems),
- **machine learning**, and
- **DeFi protocol design**.

The goal is to bring **serious control theory** into onchain finance —  
to move DeFi from _“if price > X then do Y”_ scripts to  
**principled, predictive agents**.

Along the way, a mis-committed `.env.example` and a **swiftly drained \$10 testnet wallet**  
served as a very real reminder that:

- the adversary is automated,
- mistakes are punished instantly, and
- resilient systems require both **good math** and **good operational hygiene**.

If you are a **VC, protocol, or research lab** exploring:

- agentic LP vaults,
- predictive execution on Uniswap v4,
- or DeFi × control/PDE,

`v4-KineticFlow` is intended as a serious starting point — and a live demo —  
for what that future could look like.
