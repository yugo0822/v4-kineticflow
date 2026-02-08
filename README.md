# v4-KineticFlow: Predictive Liquidity for Uniswap v4

<p align="center">
  <img src="demo.gif" alt="v4-KineticFlow Demo" width="800px">
  <br>
  <i>MPPI-driven stochastic range optimization in action on Base Sepolia.</i>
</p>
**_From reflexes to foresight: a model-predictive LP agent on Base._**

`v4-KineticFlow` is an agentic liquidity vault for **Uniswap v4** that uses  
**Model Predictive Path Integral (MPPI)** control to _predict_ future prices and  
rebalance concentrated liquidity ranges **before** the market moves there.

Built on **Base Sepolia** with **Uniswap v4 Hooks** and a Python control engine,  
the system continuously:

- Simulates **$10^4+$** stochastic price paths.
- Scores each sequence with a **control-theoretic cost**.
- Executes the optimal range update on-chain via `PositionManager.modifyLiquidities`.

This is not just another “reactive” rebalance bot.  
It is a **stochastic optimal control policy** running **inside a DeFi protocol**.

---

## TL;DR

- **Problem**: Today’s LP strategies are mostly _reactive_. They chase price after it moves, leak fees when out-of-range, and ignore the distribution of future prices.
- **Idea**: Bring **stochastic optimal control** into Uniswap v4. Use **MPPI** to simulate thousands of possible future paths and place liquidity **where the price is most likely to be**, not where it is now.
- **Implementation**:
  - Built using the Uniswap Foundation v4-template as a foundation
  - Uniswap v4 stack + Hook on **Base Sepolia**.
  - Python MPPI engine controlling **ticks**, not prices.
  - Arbitrage bot that anchors pool price to an external oracle.
  - Streamlit dashboard for real-time PnL & gas metrics.
- **Outcome**: A research-grade LP agent demonstrating the feasibility of predictive control on a live L2.

---

## The Problem: Reactive Concentrated Liquidity

Uniswap v3/v4 gave LPs **concentrated liquidity**, but left them with a hard problem:

- **Impermanent Loss (IL)** grows when LPs are stuck out-of-range.
- **Capital efficiency** collapses if ranges are too wide “just to be safe”.
- **Most bots are reactive**: They behave like _PID controllers_ in a world that actually needs **predictive control**.

For an LP vault to be truly competitive, it must optimize **fee income − gas cost − IL** over a horizon, anticipating where price _will be_.

---

## The Solution: An MPPI Engine on Uniswap v4

### MPPI Theory in One Paragraph

MPPI (Model Predictive Path Integral control) is a sampling-based stochastic optimal control method. Instead of solving the Hamilton–Jacobi–Bellman PDE exactly, MPPI:

1. Samples many control sequences $\{u_{0:T}^k\}_{k=1}^K$.
2. Simulates trajectories $x_{0:T}^k$ with dynamics $x_{t+1} = f(x_t, u_t^k, \xi_t^k)$.
3. Computes path costs:
   $$S^k = \sum_{t=0}^{T-1} \ell(x_t^k, u_t^k) + \phi(x_T^k) + \lambda \Sigma||u_t||\Sigma^{-1}$$
4. Reweights controls using an exponential weighting:
   $$u_t^\star = \frac{\sum_k \exp(-S^k / \lambda)\, u_t^k}{\sum_k \exp(-S^k / \lambda)}$$
5. Applies only the **first element** $u_0^\star$ on-chain, then repeats (receding horizon).

### Control State: Working in Tick Space

We formulate the MPPI problem directly in **ticks**, the discrete unit of Uniswap v4:

- **State** $x_t \in \mathbb{R}^4$:
  $$x_t = [t_\text{mkt}(t),\ t_\text{pool}(t),\ t_\text{center}(t),\ w_\text{ticks}(t)]$$
- **Control** $u_t \in \mathbb{R}^2$:
  $$u_t = [\Delta t_\text{center},\ \Delta w_\text{ticks}]$$

The corresponding **target Uniswap v4 range** is:
$$t_\text{lower} = t_\text{center}^{\text{new}} - w_\text{ticks}^{\text{new}},\quad t_\text{upper} = t_\text{center}^{\text{new}} + w_\text{ticks}^{\text{new}}$$

### Trajectory Cost and Dynamics

The **path cost** $S^k$ in MPPI is the sum of **stage costs** over the horizon, a **terminal cost**, and a **control penalty**. The implementation in `dashboard/optimizer/` uses the following.

**Dynamics** (`utils.py`):

To simulate realistic market behavior, we employ a **Jump-Diffusion model** for external price discovery.

### 1. Market Tick Dynamics ($t_\text{mkt}$)
* **Transition**: $t_\text{mkt}(t+1) = t_\text{mkt}(t) + \delta_t$
* **Drift & Diffusion**: $\delta_t = \frac{\log(\text{price factor})}{\log(1.0001)}$
* **Stochastic Process**:
    * **Base**: Geometric Brownian Motion (GBM) with $\sigma=0.02, \mu=0$.
    * **Jumps**: Probability of $0.05$ per step, with size $\exp(0.1 \cdot z)$.
    * **Clamping**: Factors are clamped to $[0.7,\, 1.3]$ to eliminate extreme outliers while maintaining high volatility for stress testing.

### 2. Liquidity Range Evolution ($t_\text{center}, w_\text{ticks}$)
These represent the control variables adjusted by the MPPI agent to optimize liquidity concentration.

* **Center Position**: $t_\text{center}(t+1) = t_\text{center}(t) + \Delta t_\text{center}$
* **Range Width**: $w_\text{ticks}(t+1) = \max(w_\text{ticks}(t) + \Delta w_\text{ticks},\, 120)$
    > **Note**: The lower bound of 120 ticks is enforced to remain compatible with Uniswap v4 `tickSpacing = 60`.

### 3. Pool Tick Dynamics ($t_\text{pool}$)
The pool tick follows the market tick within the active liquidity range. However, it is physically constrained (clamped) by the current range boundaries.

* **Non-linear Tracking**: The speed of convergence depends on a deviation-dependent gain $k$:
$$k = 0.2 + 0.75 \tanh(2 \cdot \text{reldev})$$
* **Behavior**: The $\tanh$ function ensures that the pool price moves faster when it is significantly far from the market (e.g., after a rebalance or a jump), but remains stable when the deviation is small.

**Stage cost** $\ell(x_t,\, u_t)$ (`cost_function.py`), per step:

| Term                  | Formula / condition                                                                                                  | Role                                                                              |
| --------------------- | -------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Fee reward            | $-0.01$ if pool tick is in range, else $0$                                                                           | Encourages in-range liquidity.                                                    |
| IL / tracking         | $5 \times 10^{-5} \cdot (t_\text{mkt} - t_\text{pool})^2$                                                            | Penalizes pool–market tick divergence (proxy for IL).                             |
| Boundary hit          | $0.05$ if pool tick is within 1 tick of lower/upper edge                                                             | Discourages being pinned at range edges.                                          |
| Proximity             | $2 \times 10^{-5} \cdot \text{proximity}^2$, with $\text{proximity} = \max(0,\, 120 - \text{distancetoedge})$ (tick) | Prefers keeping the pool tick at least ~120 ticks away from range edges (buffer). |
| Market outside        | $5 \times 10^{-4} \cdot d^2$ if market tick is outside range, $d$ = distance to nearest bound                        | Strong penalty when the market has left the current range.                        |
| Rebalance (gas proxy) | $0.002$ if $\|\Delta t_\text{center}\| + \|\Delta w_\text{ticks}\| > 120$ ticks                                      | Approximates gas cost for non-trivial rebalances.                                 |

**Terminal cost** $\phi(x_T)$:

- **Distance**: $5 \times 10^{-5} \cdot (t_\text{mkt} - t_\text{center})^2$ — penalizes end-of-horizon misalignment of market and range center.
- **Width**: $1 \times 10^{-4} \cdot w_\text{ticks}$ — penalizes wide ranges (lower capital efficiency).

**Total path cost** (controller): $S^k = \sum_{t=0}^{T-1} \ell(x_t^k,\, u_t^k) + \phi(x_T^k) + \lambda \sum_{t=0}^{T-1} \text{actioncost}(u_t^k)$. Control inputs are weighted by the same $\lambda$ and noise covariance used in the MPPI update. All cost weights and the dynamics parameters above are defined in `dashboard/optimizer/utils.py` and `dashboard/optimizer/cost_function.py`.

---

## System Architecture

The system bridges Dockerized off-chain predictive compute with on-chain Uniswap v4 Hooks on Base Sepolia. This containerized architecture ensures reproducible, high-performance MPPI control for real-time, autonomous liquidity management.

<p align="center">
  <img src="system_architecture.drawio.png" alt="System Architecture" width="800px">
</p>

---

## Demo

Our demo showcases the **KineticFlow-MPPI** agent autonomously managing liquidity on a Uniswap v4 pool.

### Environment & Simulation Setup
To rigorously test the control logic within the hackathon timeframe, we established the following environment:

- **Mock Market Prices**: We used a stochastic Jump-Diffusion model to generate mock external market prices. This allows us to simulate various market regimes, from steady trends to high-volatility shocks, ensuring the MPPI controller can adapt to diverse scenarios.
- **Active Arbitrage Bot**: To create a realistic trading environment, we deployed a dedicated **Arbitrage Bot** that constantly monitors the price gap between our Uniswap v4 pool and the mock external market. This bot executes trades to rebalance the pool price, providing the "market pressure" necessary to test our liquidity management under real-world conditions.
- **Visual Feedback**: The demo includes a real-time dashboard visualizing the MPPI's proposed range shifts vs. the actual pool price and the resulting PnL.

> **Note**: Crucially, for this demonstration, we intentionally simulated a highly volatile market environment to stress-test the algorithm under more stringent conditions, proving its robustness against rapid price swings.

[Link to Demo Video] https://youtu.be/WbSvjjo0nMw

---

##  Roadmap & Future Vision

### Short-Term Vision: Optimization & Transparency 
- **Dynamic Volatility Adaptation**: Integrate an algorithm that estimates real-time market volatility and reflects it directly into the MPPI dynamics. This ensures optimal liquidity ranges even during high-slippage environments.
- **High-Fidelity Profit UI**: Develop a more intuitive dashboard that visualizes Net PnL, earned fees, and impermanent loss in real-time.
- **Comparative Benchmarking**: Clearly demonstrate the superiority of `KineticFlow-MPPI` by providing side-by-side performance data against naive methods (e.g., static v3 positions or periodic recentering).

### Mid-Term Vision: Expansion & Accessibility
- **Omnichain Interoperability**: Enable cross-chain interactions, allowing the MPPI controller to manage liquidity across multiple networks (e.g., Base, Arbitrum, and Ethereum Mainnet) simultaneously.
- **Frictionless UX/UI**: Drastically simplify the interface so that any user, regardless of their background in control theory, can deploy sophisticated liquidity strategies with just a few clicks.
- **Intelligent Presets**: Implement "One-Click Strategies" based on risk profiles (Low/Medium/High volatility targets).

### Long-Term Vision: The Control Layer of DeFi
Our ultimate goal is to establish **KineticFlow** as a general-purpose control layer for the entire DeFi ecosystem.
- **DeFi as a Control System**: We envision a world where LP vaults, liquidators, and routers are all modeled as interacting control systems.
- **Mathematical Foundation**: Instead of relying on ad-hoc heuristics, DeFi will leverage 50 years of proven research in **Control Theory, Partial Differential Equations (PDEs), and Stochastic Calculus** ($dX_t = f(X_t, u_t)dt + g(X_t, u_t)dW_t$).
- **Institutional-Grade Infrastructure**: Providing the mathematical rigor required for institutional capital to participate safely in decentralized markets.

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
2. Optional: set `ETH_MAINNET_RPC_URL` to show ENS names (e.g. `vitalik.eth`) next to contract addresses in the Streamlit dashboard.
3. Ensure `.env` is **never committed**.

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

### 📂 Key Code Locations
- **MPPI Controller**: `dashboard/optimizer/controller.py`
- **Uniswap v4 Hook**: `src/KineticFlowHook.sol` (or your hook file name)
- **On-chain Execution Logic**: `bot/mppi_bot.py`

---

## Applicable prizes

**Uniswap Foundation — Agentic Finance:** v4-KineticFlow is an agent that programmatically manages concentrated liquidity on Uniswap v4: the MPPI controller computes optimal tick ranges and executes them on-chain via `PositionManager.modifyLiquidities` on Base Sepolia, with a v4 Hook and full transaction logging for transparency.

**ENS:** The dashboard integrates ENS by resolving and displaying primary .eth names next to contract and wallet addresses (using Ethereum mainnet for resolution). ENS-specific code lives in `dashboard/ens_utils.py` and the Contract addresses section in the Streamlit app; set `ETH_MAINNET_RPC_URL` in `.env` to enable resolution.

---

## Team / Author

This project is built by a **Yugo Sudo** Kyoto University student specializing in:

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


[e-mail] west18u5@gmail.com

[LinkedIn] https://www.linkedin.com/in/yugo-sudo-9a5190362/
