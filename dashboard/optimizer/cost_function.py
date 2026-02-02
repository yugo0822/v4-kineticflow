import torch

def stage_cost(state: torch.Tensor, action: torch.Tensor) -> torch.Tensor:
    """
    state: (num_samples, num_samples_expect, 4)
    action: (num_samples, num_samples_expect, 2)
    """
    p_market = state[..., 0]
    p_pool   = state[..., 1]
    p_center = state[..., 2]
    width    = state[..., 3]

    # レンジの境界計算
    lower = p_center - (width / 2)
    upper = p_center + (width / 2)

    # 1. 手数料収益 (マイナスのコスト)
    # プール価格がレンジ内にあるときのみ収益が発生
    in_range = (p_pool > lower) & (p_pool < upper)
    fee_reward = torch.where(in_range, -0.01, 0.0) # 定数値または濃度に応じた式

    # 2. インパーマネントロス (IL) / 乖離ペナルティ
    # 市場価格とプール価格のズレをコストとする
    il_cost = 0.5 * torch.pow((p_market - p_pool) / p_market, 2)

    # 3. リバランス実行コスト (ガス代)
    # アクション（変化量）が一定以上のときだけ固定コストをかける
    rebalance_cost = torch.where(torch.abs(action).sum(dim=-1) > 1e-4, 0.005, 0.0)

    return fee_reward + il_cost + rebalance_cost

def terminal_cost(state: torch.Tensor) -> torch.Tensor:
    """
    state: (num_samples, num_samples_expect, 4)
    """
    p_market = state[..., 0]
    p_center = state[..., 2]
    width    = state[..., 3]

    # 最終的な価格とレンジ中心のズレに対するペナルティ
    dist_cost = torch.pow((p_market - p_center) / p_market, 2)
    
    # 幅が広すぎることに対するペナルティ (資本効率の低下を表現)
    width_penalty = 0.001 * width

    return dist_cost + width_penalty