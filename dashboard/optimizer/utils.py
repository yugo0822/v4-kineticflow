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
    # --- 1. 拡散項 (Diffusion / GBM成分) ---
    # 日々の細かな価格の揺れ。sigma=0.02なら1ステップ平均2%の変動
    sigma = 0.02 
    mu = 0.0    # 短期予測なのでドリフトは0と仮定することが多い
    
    # 標準正規乱数 N(0, 1)
    z_diffusion = torch.randn(num_samples_expect, horizon, device=device, dtype=dtype)
    diffusion_ret = mu + sigma * z_diffusion

    # --- 2. 跳躍項 (Jump / スパイク成分) ---
    # 突発的な大変動。これが「ジャンプ拡散モデル」の核心
    jump_prob = 0.05    # 各ステップで5%の確率でジャンプ（急変）が発生
    jump_std = 0.10     # ジャンプが発生した時の変動幅（10%級の衝撃）

    # 0~1の均一乱数から、確率に基づいてジャンプの発生を判定（ベルヌーイ試行の近似）
    jump_mask = (torch.rand(num_samples_expect, horizon, device=device, dtype=dtype) < jump_prob).to(dtype)
    
    # ジャンプが発生した箇所にだけ、大きなノイズを乗せる
    z_jump = torch.randn(num_samples_expect, horizon, device=device, dtype=dtype)
    jump_ret = jump_mask * (z_jump * jump_std)

    # --- 3. 合計収益率の算出 ---
    # params = 1 + (通常の揺れ) + (突発的なジャンプ)
    params = 1.0 + diffusion_ret + jump_ret

    return params

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