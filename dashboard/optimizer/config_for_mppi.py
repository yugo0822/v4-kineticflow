import torch

MPPI_CONFIG = {
    "PROPOSE": True,
    "horizon": 10,
    "num_samples": 1000,
    "num_samples_expect": 20,
    "dim_state": 2,    # [pool_price, external_price]
    "dim_control": 2,  # [relative_center_shift, relative_width]
    "u_min": torch.tensor([-0.2, 0.05]),
    "u_max": torch.tensor([0.2, 0.5]),
    "sigmas": torch.tensor([0.05, 0.1]),
    "lambda_": 1.0,
}