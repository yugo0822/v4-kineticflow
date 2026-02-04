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

def generate_jump_diffusion_parameter_seq(
    num_samples_expect: int, 
    horizon: int, 
    device: torch.device, 
    dtype: torch.dtype
) -> torch.Tensor:
    """
    S_MPPIクラスの self._generate_random_parameter_seq に対応する関数。
    (num_samples_expect, horizon) の形状で「1 + 収益率」を返す。
    """
    # NOTE:
    # We return a *multiplicative* price factor so it is strictly positive.
    # In tick-space dynamics we convert it via: Δtick = log(factor) / log(1.0001)

    # --- 1. 拡散項 (Diffusion / GBM factor) ---
    sigma = 0.02
    mu = 0.0
    z = torch.randn(num_samples_expect, horizon, device=device, dtype=dtype)
    diffusion_factor = torch.exp(mu + sigma * z)  # strictly positive

    # --- 2. 跳躍項 (Jump factor) ---
    jump_prob = 0.05
    jump_sigma = 0.10
    jump_mask = (torch.rand(num_samples_expect, horizon, device=device, dtype=dtype) < jump_prob)
    z_jump = torch.randn(num_samples_expect, horizon, device=device, dtype=dtype)
    # Jump is multiplicative: exp(jump_sigma * z_jump) when it happens, else 1
    jump_factor = torch.ones(num_samples_expect, horizon, device=device, dtype=dtype)
    jump_factor = torch.where(jump_mask, torch.exp(jump_sigma * z_jump), jump_factor)

    params = diffusion_factor * jump_factor  # > 0
    # Avoid extreme outliers that dominate MPPI weighting
    params = torch.clamp(params, min=0.7, max=1.3)
    return params

def uniswap_dynamics(state: torch.Tensor, action: torch.Tensor, params: torch.Tensor) -> torch.Tensor:
    """
    state: (num_samples, num_samples_expect, 4) 
           -> [t_market, t_pool, t_center, width_ticks]
    action: (num_samples, num_samples_expect, 2)
           -> [delta_t_center, delta_width_ticks]
    params: (num_samples, num_samples_expect) 
           -> 市場価格の変動率 (multiplicative factor, strictly > 0)
    """
    # 状態の分解
    t_market = state[..., 0]
    t_pool   = state[..., 1]
    t_center = state[..., 2]
    w_ticks  = state[..., 3]

    # 1. 市場 tick の更新
    # Δtick = log(price_factor) / log(1.0001)
    log_base = math.log(1.0001)
    delta_t_market = torch.log(params) / log_base
    next_t_market = t_market + delta_t_market

    # 2. レンジ（tick）更新
    next_t_center = t_center + action[..., 0]
    next_w_ticks  = w_ticks + action[..., 1]
    # keep width positive and meaningful relative to tickSpacing=60
    next_w_ticks = torch.clamp(next_w_ticks, min=120.0)

    lower = next_t_center - (next_w_ticks / 2.0)
    upper = next_t_center + (next_w_ticks / 2.0)

    # 3. プール tick の更新（レンジ内追従、レンジ外は境界に張り付く）
    # Tracking speed depends on deviation relative to width.
    rel_dev = torch.abs(next_t_market - t_pool) / torch.clamp(next_w_ticks, min=1e-6)
    k = 0.2 + 0.75 * torch.tanh(2.0 * rel_dev)   # in (0.2, ~0.95)

    t_pool_raw = t_pool + k * (next_t_market - t_pool)
    next_t_pool = torch.clamp(t_pool_raw, lower, upper)

    # 次の状態を結合して返す
    return torch.stack([next_t_market, next_t_pool, next_t_center, next_w_ticks], dim=-1)