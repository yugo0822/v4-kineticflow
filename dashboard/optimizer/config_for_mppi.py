import torch

MPPI_CONFIG = {
    "PROPOSE": True,
    "horizon": 10,
    "num_samples": 1000,
    "num_samples_expect": 20,
    "dim_state": 4,    # [P_market, P_pool, P_center, width]
    "dim_control": 2,  # [delta_P_center, delta_width]
    "u_min": torch.tensor([-50.0, -100.0]), # 大きな価格変動に対応するため範囲を広げる
    "u_max": torch.tensor([50.0, 100.0]),
    "sigmas": torch.tensor([10.0, 20.0]),   # 探索ノイズも大きめに
    "lambda_": 1.0,
}