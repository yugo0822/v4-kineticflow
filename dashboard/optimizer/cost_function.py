import torch

def stage_cost(state: torch.Tensor, action: torch.Tensor) -> torch.Tensor:
    """
    state: (..., 4) -> [t_market, t_pool, t_center, width_ticks]
    action: (..., 2)
    """
    t_market = state[..., 0]
    t_pool = state[..., 1]
    t_center = state[..., 2]
    w_ticks = state[..., 3]

    lower = t_center - (w_ticks / 2)
    upper = t_center + (w_ticks / 2)

    in_range = (t_pool > lower) & (t_pool < upper)
    fee_reward = torch.where(in_range, -0.01, 0.0)

    il_cost = 5e-5 * torch.pow((t_market - t_pool), 2)

    eps = 1.0
    boundary_hit = (t_pool <= lower + eps) | (t_pool >= upper - eps)
    boundary_hit_cost = torch.where(boundary_hit, 0.05, 0.0)

    buffer_ticks = 120.0
    dist_to_edge = torch.minimum(t_pool - lower, upper - t_pool)
    proximity = torch.relu(buffer_ticks - dist_to_edge)
    proximity_cost = 2e-5 * proximity * proximity

    market_outside = (t_market < lower) | (t_market > upper)
    market_outside_dist = torch.where(t_market < lower, lower - t_market, t_market - upper)
    market_outside_cost = torch.where(market_outside, 5e-4 * market_outside_dist * market_outside_dist, 0.0)

    rebalance_cost = torch.where(torch.abs(action).sum(dim=-1) > 120.0, 0.002, 0.0)

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
    state: (..., 4) -> [t_market, t_pool, t_center, width_ticks]
    """
    t_market = state[..., 0]
    t_center = state[..., 2]
    w_ticks = state[..., 3]

    dist_cost = 5e-5 * torch.pow((t_market - t_center), 2)
    width_penalty = 1e-4 * w_ticks

    return dist_cost + width_penalty