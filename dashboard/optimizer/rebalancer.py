import torch
from .controller import S_MPPI
from .config_for_mppi import MPPI_CONFIG
from .cost_function import stage_cost, terminal_cost
from .utils import (
    uniswap_dynamics,
    generate_random_parameter_seq,
    generate_constant_parameter_seq,
    price_to_tick,
    clamp_ticks,
)

class MPPIRebalancer:
    def __init__(self, device: str = "cpu"):
        cfg = MPPI_CONFIG
        self.controller = S_MPPI(
            PROPOSE=cfg["PROPOSE"],
            horizon=cfg["horizon"],
            num_samples=cfg["num_samples"],
            num_samples_expect=cfg["num_samples_expect"],
            dim_state=cfg["dim_state"],
            dim_control=cfg["dim_control"],
            dynamics=uniswap_dynamics,
            generate_random_parameter_seq=generate_random_parameter_seq,
            generate_constant_parameter_seq=generate_constant_parameter_seq,
            stage_cost=stage_cost,
            terminal_cost=terminal_cost,
            u_min=cfg["u_min"],
            u_max=cfg["u_max"],
            sigmas=cfg["sigmas"],
            lambda_=cfg["lambda_"],
            device=torch.device(device),
        )

    def compute_target_range(self, pool_price: float, external_price: float):
        state = torch.tensor([pool_price, external_price], dtype=torch.float32)
        optimal_action_seq = self.controller(state)
        u0 = optimal_action_seq[0]  # first step control

        center_shift = float(u0[0])
        width_rel = float(u0[1])

        center_price = external_price * (1.0 + center_shift)
        width_rel = max(0.05, min(width_rel, 0.5))
        lower_price = center_price * (1.0 - width_rel)
        upper_price = center_price * (1.0 + width_rel)

        tick_lower = price_to_tick(lower_price)
        tick_upper = price_to_tick(upper_price)
        tick_lower, tick_upper = clamp_ticks(tick_lower, tick_upper)

        return tick_lower, tick_upper