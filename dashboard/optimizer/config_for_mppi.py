import torch

MPPI_CONFIG = {
    "PROPOSE": True,
    "horizon": 1,
    "num_samples": 10000,
    "num_samples_expect": 20,
    # Tick-based state/control to avoid tickSpacing rounding wiping out small changes
    "dim_state": 4,    # [t_market, t_pool, t_center, width_ticks]
    "dim_control": 2,  # [delta_t_center, delta_width_ticks]
    # Tick spacing is 60 in this project; keep controls on the order of a few hundred ticks
    "u_min": torch.tensor([-600.0, -1200.0]),
    "u_max": torch.tensor([600.0, 1200.0]),
    "sigmas": torch.tensor([120.0, 240.0]),
    "lambda_": 1.0,
}