import torch

def stage_cost(state: torch.Tensor, action: torch.Tensor) -> torch.Tensor:
    """
    state: (num_samples, num_samples_expect, 4)
    action: (num_samples, num_samples_expect, 2)
    """
    # Tick-based state:
    # [t_market, t_pool, t_center, width_ticks]
    t_market = state[..., 0]
    t_pool   = state[..., 1]
    t_center = state[..., 2]
    w_ticks  = state[..., 3]

    # レンジの境界計算
    lower = t_center - (w_ticks / 2)
    upper = t_center + (w_ticks / 2)

    # 1) Fee reward proxy (negative cost)
    # In-range only. Probabilistic "in_range" is achieved by MPPI expectation over rollouts.
    in_range = (t_pool > lower) & (t_pool < upper)
    fee_reward = torch.where(in_range, -0.01, 0.0)

    # 2) Tracking / divergence penalty
    # Tick is log-price, so squared tick deviation is a natural proxy.
    il_cost = 1e-5 * torch.pow((t_market - t_pool), 2)

    # 3) Range-risk penalties (make "narrow ranges hit boundaries more often" matter)
    # (a) Boundary hit penalty: being pinned at lower/upper is bad (no liquidity beyond range).
    eps = 1.0  # 1 tick tolerance
    boundary_hit = (t_pool <= lower + eps) | (t_pool >= upper - eps)
    boundary_hit_cost = torch.where(boundary_hit, 0.05, 0.0)

    # (b) Proximity penalty: even if still in-range, hugging edges increases chance of going OOR.
    # Encourage keeping a buffer from both edges.
    # Buffer size is in ticks (tickSpacing is 60; 2*60=120 is a good default).
    buffer_ticks = 120.0
    dist_to_edge = torch.minimum(t_pool - lower, upper - t_pool)
    proximity = torch.relu(buffer_ticks - dist_to_edge)
    proximity_cost = 2e-5 * proximity * proximity

    # (c) Market-outside penalty: if the *market* is outside the range, we are missing the market.
    market_outside = (t_market < lower) | (t_market > upper)
    market_outside_dist = torch.where(t_market < lower, lower - t_market, t_market - upper)
    market_outside_cost = torch.where(market_outside, 1e-4 * market_outside_dist * market_outside_dist, 0.0)

    # 4) Rebalance execution cost (gas proxy)
    # Only charge when the action is meaningfully non-zero (tick-based).
    rebalance_cost = torch.where(torch.abs(action).sum(dim=-1) > 60.0, 0.005, 0.0)

    return (
        fee_reward
        + il_cost
        + boundary_hit_cost
        + proximity_cost
        + market_outside_cost
        + rebalance_cost
    )

def terminal_cost(state: torch.Tensor) -> torch.Tensor:
    """
    state: (num_samples, num_samples_expect, 4)
    """
    t_market = state[..., 0]
    t_center = state[..., 2]
    w_ticks  = state[..., 3]

    # 最終的な価格とレンジ中心のズレに対するペナルティ
    dist_cost = 1e-5 * torch.pow((t_market - t_center), 2)
    
    # 幅が広すぎることに対するペナルティ (資本効率の低下を表現)
    width_penalty = 1e-4 * w_ticks

    return dist_cost + width_penalty