import math
import torch

def price_to_tick(price: float) -> int:
    return int(round(math.log(price) / math.log(1.0001)))

def tick_to_price(tick: int) -> float:
    return 1.0001 ** tick

def clamp_ticks(tick_lower: int, tick_upper: int) -> tuple[int, int]:
    from math import floor, ceil
    MIN_TICK, MAX_TICK = -887272, 887272
    tick_lower = max(MIN_TICK, min(tick_lower, tick_upper - 1))
    tick_upper = min(MAX_TICK, max(tick_upper, tick_lower + 1))
    return tick_lower, tick_upper

def generate_random_parameter_seq(num_samples_expect, horizon, device, dtype):
    return torch.randn(num_samples_expect, horizon, device=device, dtype=dtype)

def generate_constant_parameter_seq(num_samples_expect, horizon, device, dtype):
    return torch.zeros(num_samples_expect, horizon, device=device, dtype=dtype)

def uniswap_dynamics(state: torch.Tensor, action: torch.Tensor, params: torch.Tensor) -> torch.Tensor:
    """
    state: (num_samples, num_samples_expect, 4) 
           -> [P_market, P_pool, P_center, width]
    action: (num_samples, num_samples_expect, 2)
           -> [delta_P_center, delta_width]
    params: (num_samples, num_samples_expect) 
           -> 市場価格の変動率 (1 + ret)
    """
    # 状態の分解
    p_market = state[..., 0]
    p_pool   = state[..., 1]
    p_center = state[..., 2]
    width    = state[..., 3]

    # 1. 市場価格の更新 (params = 1 + return)
    next_p_market = p_market * params

    # 2. プール価格の更新 (裁定取引による追従)
    # 係数 0.5 は裁定の速さ。実際にはもっと速い（1.0に近い）
    next_p_pool = p_pool + 0.8 * (next_p_market - p_pool)

    # 3. レンジ設定の更新 (アクションを適用)
    next_p_center = p_center + action[..., 0]
    next_p_width  = width + action[..., 1]
    
    # 下限値のクリッピング (幅がマイナスにならないように)
    next_p_width = torch.clamp(next_p_width, min=0.01)

    # 次の状態を結合して返す
    return torch.stack([next_p_market, next_p_pool, next_p_center, next_p_width], dim=-1)