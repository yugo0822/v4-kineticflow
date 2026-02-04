import torch
from .controller import S_MPPI
from .config_for_mppi import MPPI_CONFIG
from .cost_function import stage_cost, terminal_cost
from .utils import (
    uniswap_dynamics,
    generate_jump_diffusion_parameter_seq,
    generate_constant_parameter_seq,
    price_to_tick,
    clamp_ticks,
    tick_to_price,
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
            # Keep consistent with dashboard/mppi_bot.py
            generate_random_parameter_seq=generate_jump_diffusion_parameter_seq,
            generate_constant_parameter_seq=generate_constant_parameter_seq,
            stage_cost=stage_cost,
            terminal_cost=terminal_cost,
            u_min=cfg["u_min"],
            u_max=cfg["u_max"],
            sigmas=cfg["sigmas"],
            lambda_=cfg["lambda_"],
            device=torch.device(device),
        )

    @staticmethod
    def _truncate_tick(tick: int, tick_spacing: int) -> int:
        # Round toward negative infinity to keep ticks aligned
        return (tick // tick_spacing) * tick_spacing

    def compute_action(
        self,
        external_price: float,
        pool_price: float,
        tick_lower: int,
        tick_upper: int,
        tick_spacing: int,
    ) -> tuple[float, float]:
        """
        Compute the immediate MPPI control action.

        State definition (dim_state=4):
          [P_market, P_pool, P_center, width]
        Control (dim_control=2):
          [ΔP_center, Δwidth]  (both in price units, not ticks)
        """
        price_lower = tick_to_price(int(tick_lower))
        price_upper = tick_to_price(int(tick_upper))
        p_center = (price_lower + price_upper) / 2.0
        width = max(0.01, price_upper - price_lower)

        state = torch.tensor(
            [float(external_price), float(pool_price), float(p_center), float(width)],
            dtype=torch.float32,
        )

        current_action, _ = self.controller.forward(state)
        delta_center = float(current_action[0].item())
        delta_width = float(current_action[1].item())
        return delta_center, delta_width

    def compute_target_range(
        self,
        external_price: float,
        pool_price: float,
        tick_lower: int,
        tick_upper: int,
        tick_spacing: int,
    ) -> tuple[int, int]:
        """
        Compute target [tickLower, tickUpper] using the immediate MPPI action.
        """
        price_lower = tick_to_price(int(tick_lower))
        price_upper = tick_to_price(int(tick_upper))
        current_center = (price_lower + price_upper) / 2.0
        current_width = max(0.01, price_upper - price_lower)

        delta_center, delta_width = self.compute_action(
            external_price=external_price,
            pool_price=pool_price,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            tick_spacing=tick_spacing,
        )

        new_center = current_center + delta_center
        new_width = max(0.01, current_width + delta_width)
        lower_price = new_center - (new_width / 2.0)
        upper_price = new_center + (new_width / 2.0)

        tick_lower = price_to_tick(lower_price)
        tick_upper = price_to_tick(upper_price)
        tick_lower, tick_upper = clamp_ticks(tick_lower, tick_upper)

        tick_lower = self._truncate_tick(tick_lower, tick_spacing)
        tick_upper = self._truncate_tick(tick_upper, tick_spacing)
        if tick_lower >= tick_upper:
            tick_upper = tick_lower + tick_spacing

        return int(tick_lower), int(tick_upper)