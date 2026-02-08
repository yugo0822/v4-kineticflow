"""
  サンプリング間隔がi.i.d.である場合のMPPIコントローラ
"""

from __future__ import annotations

from typing import Callable, Tuple

import time
import torch
import torch.nn as nn
from torch.distributions.multivariate_normal import MultivariateNormal


class S_MPPI(nn.Module):
    """
    Model Predictive Path Integral Control,
    J. Williams et al., T-RO, 2017.
    """

    def __init__(
        self,
        PROPOSE: bool,
        horizon: int,
        num_samples: int,
        num_samples_expect: int,
        dim_state: int,
        dim_control: int,
        dynamics: Callable[[torch.Tensor, torch.Tensor], torch.Tensor],
        generate_random_parameter_seq: Callable[[int], torch.Tensor],
        generate_constant_parameter_seq: Callable[[int], torch.Tensor],
        stage_cost: Callable[[torch.Tensor, torch.Tensor], torch.Tensor],
        terminal_cost: Callable[[torch.Tensor], torch.Tensor],
        u_min: torch.Tensor,
        u_max: torch.Tensor,
        sigmas: torch.Tensor,
        lambda_: float,
        device=torch.device("cuda"),
        dtype=torch.float32,
        seed: int = 42,
    ) -> None:
        """
        :param horizon: Predictive horizon length.
        :param delta: predictive horizon step size (seconds).
        :param num_samples: Number of samples.
        :param dim_state: Dimension of state.
        :param dim_control: Dimension of control.
        :param dynamics: Dynamics model.
        :param generate_random_parameter_seq: random parameter generator.
        :param stage_cost: Stage cost.
        :param terminal_cost: Terminal cost.
        :param u_min: Minimum control.
        :param u_max: Maximum control.
        :param sigmas: Noise standard deviation for each control dimension.
        :param lambda_: temperature parameter.
        :param device: Device to run the solver.
        :param dtype: Data type to run the solver.
        :param seed: Seed for torch.
        """

        super().__init__()

        # torch seed
        torch.manual_seed(seed)

        # check dimensions
        assert u_min.shape == (dim_control,)
        assert u_max.shape == (dim_control,)
        assert sigmas.shape == (dim_control,)
        # assert num_samples % batch_size == 0 and num_samples >= batch_size

        # device and dtype GPUが使用可能かつデバイスとしてGPUが指定されている場合、計算をGPU上で行うように設定します
        if torch.cuda.is_available() and device == torch.device("cuda"):
            self._device = torch.device("cuda")
        else: #GPUが利用不可能な場合は、計算をCPU上で行うように設定します。
            self._device = torch.device("cpu")
        self._dtype = dtype #使用するデータの方を設定する

        # set parameters
        self._PROPOSE = PROPOSE
        self._horizon = horizon
        self._num_samples = num_samples
        self._num_samples_expect = num_samples_expect
        self._dim_state = dim_state
        self._dim_control = dim_control
        self._dynamics = dynamics
        self._generate_random_parameter_seq = generate_random_parameter_seq
        self._generate_constant_parameter_seq = generate_constant_parameter_seq
        self._stage_cost = stage_cost
        self._terminal_cost = terminal_cost
        self._u_min = u_min.clone().detach().to(self._device, self._dtype)
        self._u_max = u_max.clone().detach().to(self._device, self._dtype)
        self._sigmas = sigmas.clone().detach().to(self._device, self._dtype)
        self._lambda = lambda_

        # noise distribution
        zero_mean = torch.zeros(dim_control, device=self._device, dtype=self._dtype)
        initial_covariance = torch.diag(sigmas**2).to(self._device, self._dtype)
        self._inv_covariance = torch.inverse(initial_covariance).to(
            self._device, self._dtype
        )

        self._noise_distribution = MultivariateNormal(
            loc=zero_mean, covariance_matrix=initial_covariance
        )
        self._sample_shape = torch.Size([self._num_samples, self._horizon])

        # sampling with reparameting trick
        self._action_noises = self._noise_distribution.rsample(
            sample_shape=self._sample_shape
        )

        zero_mean_seq = torch.zeros(
            self._horizon, self._dim_control, device=self._device, dtype=self._dtype
        )
        self._perturbed_action_seqs = torch.clamp(
            zero_mean_seq + self._action_noises, self._u_min, self._u_max
        )

        self._previous_action_seq = zero_mean_seq

        # inner variables
        self._state_seq_batch = torch.zeros(
            self._num_samples,
            self._horizon + 1,
            self._dim_state,
            device=self._device,
            dtype=self._dtype,
        )
        self._weights = torch.zeros(
            self._num_samples, device=self._device, dtype=self._dtype
        )
        self._random_parameter_seq = torch.zeros(
            self._num_samples_expect, self._horizon
        )
##################################################################################33
###############                     main                     ################3
#################################################################################33
    def forward(self, state: torch.Tensor) -> Tuple[Tuple[torch.Tensor, torch.Tensor], float]:#forwardにstate歯科引数がなくても、事前入力系列は更新され続ける。インスタンスが作られたあと、インスタンスが存在する限り値が保持され更新され続ける
        assert state.shape == (self._dim_state,)

        if not torch.is_tensor(state):
            state = torch.tensor(state, device=self._device, dtype=self._dtype)
            #pythonにおけるテンソルとはpytorchが提供している特殊な型。テンソル型はGPUを用いて演算が可能。行列みたいなもの
        else:
            if state.device != self._device or state.dtype != self._dtype:
                state = state.to(self._device, self._dtype)

        mean_action_seq = self._previous_action_seq.clone().detach() #(self._horizon, self._dim_control)
        #self._previous_action_seqは前のステップで使用された入力系列を保持するテンソル。それをcone()でコピーし、新しいテンソルを生成する。detachでは新しく生成されたテンソルをpytochの自動勾配計算システムから独立させている

        # random sampling with reparametrization trick
        self._action_noises = self._noise_distribution.rsample(#(self._num_samples, self._horizon)
            sample_shape=self._sample_shape
        )
        self._perturbed_action_seqs = mean_action_seq + self._action_noises#ブロードキャスト
        # clamp actions 入力を特定の領域にクリッピングしている。範囲外の値は最小値化最大値に置き換えられる。
        self._perturbed_action_seqs = torch.clamp(
            self._perturbed_action_seqs, self._u_min, self._u_max
        )
        self._perturbed_action_seqs_exp = self._perturbed_action_seqs.unsqueeze(1).repeat(1, self._num_samples_expect, 1, 1)
        # rollout samples in parallel
        self._state_seq_batch[:, 0, :] = state.repeat(self._num_samples, 1)#各サンプルの状態系列を保持するテンソルを初期化している。#与えられた初期状態stateをサンプルの数だけ繰り返して、すべてのsンプルが同じ初期状態から開始するようにしている。
        self._state_seq_batch_exp = self._state_seq_batch.unsqueeze(1).repeat(1, self._num_samples_expect,1,1)
        
        #期待値計算のために確率過程を生成
        if self._PROPOSE == True:
            self._random_parameter_seq = self._generate_random_parameter_seq(self._num_samples_expect, self._horizon, self._device, self._dtype)
        else:
            self._random_parameter_seq = self._generate_constant_parameter_seq(self._num_samples_expect, self._horizon, self._device, self._dtype)
        
        self._random_parameter_seq_batch = self._random_parameter_seq.unsqueeze(0).repeat(self._num_samples,1,1)
###################### cal state and cost ###################################
        # 時間発展の計算
        for t in range(self._horizon):
            self._state_seq_batch_exp[:, :, t + 1, :] = self._dynamics(
                self._state_seq_batch_exp[:, :, t, :], 
                self._perturbed_action_seqs_exp[:, :, t, :], 
                self._random_parameter_seq_batch[:, :, t]
            )

        # ステージコスト・アクションコストの計算
        stage_costs_exp = torch.zeros(
            self._num_samples, self._num_samples_expect, self._horizon, 
            device=self._device, dtype=self._dtype
        )
        action_costs_exp = torch.zeros(
            self._num_samples, self._num_samples_expect, self._horizon, 
            device=self._device, dtype=self._dtype
        )

        for t in range(self._horizon):
            stage_costs_exp[:, :, t] = self._stage_cost(
                self._state_seq_batch_exp[:, :, t, :], 
                self._perturbed_action_seqs_exp[:, :, t, :]
            )
            action_costs_exp[:, :, t] = (
                mean_action_seq[t]
                @ self._inv_covariance
                @ self._perturbed_action_seqs_exp[:, :, t].transpose(-1, -2)
            )

        # 終端コストの計算
        terminal_costs_exp = self._terminal_cost(self._state_seq_batch_exp[:, :, -1, :])

        # `num_samples_expect` 
        stage_costs = stage_costs_exp.mean(dim=1)  # (num_samples, horizon)
        action_costs = action_costs_exp.mean(dim=1)  # (num_samples, horizon)
        terminal_costs = terminal_costs_exp.mean(dim=1)  # (num_samples,)

        # 総コストの計算
        costs = (
            torch.sum(stage_costs, dim=1)  # 時間方向に合計
            + terminal_costs
            + torch.sum(self._lambda * action_costs, dim=1)
        )

        ######################## cal weight ##################################
        # calculate weights
        self._weights = torch.softmax(-costs / self._lambda, dim=0)

        # find optimal control by weighted average
        optimal_action_seq = torch.sum(
            self._weights.view(self._num_samples, 1, 1) * self._perturbed_action_seqs,
            dim=0,
        )

        # update previous actions
        self._previous_action_seq = optimal_action_seq

        # Hysteresis / Deadband (tick-based)
        # control action 0: Δtick_center, action 1: Δwidth_ticks
        threshold_center = 60.0   # ~1 tickSpacing
        threshold_width = 120.0   # ~2 tickSpacing

        current_action = optimal_action_seq[0] # The action to be applied now
        
        if abs(current_action[0]) < threshold_center:
            current_action[0] = 0.0
        
        if abs(current_action[1]) < threshold_width:
            current_action[1] = 0.0
            
        return current_action, optimal_action_seq # Return both immediate action and full sequence
#########################################################################################33
#########################################################################################
    def get_top_samples(self, num_samples: int) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Get top samples.
        Args:引数(argment)
            num_samples (int): Number of state samples to get.取得したい上位サンプルの数を示す
        Returns:
            Tuple[torch.Tensor, torch.Tensor]: Tuple of top samples and their weights.
        """
        assert num_samples <= self._num_samples

        # large weights are better
        top_indices = torch.topk(self._weights, num_samples).indices #indecesはindexの複数形

        top_samples = self._state_seq_batch[top_indices]
        top_weights = self._weights[top_indices]

        top_samples = top_samples[torch.argsort(top_weights, descending=True)] #取り出した上位数個の重みとサンプルを重み順に並び変える
        top_weights = top_weights[torch.argsort(top_weights, descending=True)]

        return top_samples, top_weights
