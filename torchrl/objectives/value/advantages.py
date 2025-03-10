# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import abc
import functools
import warnings
from functools import wraps
from typing import Callable, List, Optional, Tuple, Union

import torch
from tensordict.nn import (
    dispatch,
    is_functional,
    set_skip_existing,
    TensorDictModule,
    TensorDictModuleBase,
)
from tensordict.tensordict import TensorDictBase
from torch import nn, Tensor

from torchrl.envs.utils import step_mdp

from torchrl.objectives.utils import hold_out_net
from torchrl.objectives.value.functional import (
    generalized_advantage_estimate,
    td0_return_estimate,
    td_lambda_return_estimate,
    vec_generalized_advantage_estimate,
    vec_td1_return_estimate,
    vec_td_lambda_return_estimate,
)


def _self_set_grad_enabled(fun):
    @wraps(fun)
    def new_fun(self, *args, **kwargs):
        with torch.set_grad_enabled(self.differentiable):
            return fun(self, *args, **kwargs)

    return new_fun


def _self_set_skip_existing(fun):
    @functools.wraps(fun)
    def new_func(self, *args, **kwargs):
        if self.skip_existing is not None:
            with set_skip_existing(self.skip_existing):
                return fun(self, *args, **kwargs)
        return fun(self, *args, **kwargs)

    return new_func


class ValueEstimatorBase(TensorDictModuleBase):
    """An abstract parent class for value function modules.

    Its :meth:`ValueFunctionBase.forward` method will compute the value (given
    by the value network) and the value estimate (given by the value estimator)
    as well as the advantage and write these values in the output tensordict.

    If only the value estimate is needed, the :meth:`ValueFunctionBase.value_estimate`
    should be used instead.

    """

    value_network: Union[TensorDictModule, Callable]
    value_key: Union[Tuple[str], str]

    @abc.abstractmethod
    def forward(
        self,
        tensordict: TensorDictBase,
        params: Optional[TensorDictBase] = None,
        target_params: Optional[TensorDictBase] = None,
    ) -> TensorDictBase:
        """Computes the advantage estimate given the data in tensordict.

        If a functional module is provided, a nested TensorDict containing the parameters
        (and if relevant the target parameters) can be passed to the module.

        Args:
            tensordict (TensorDictBase): A TensorDict containing the data
                (an observation key, "action", ("next", "reward"), ("next", "done") and "next" tensordict state
                as returned by the environment) necessary to compute the value estimates and the TDEstimate.
                The data passed to this module should be structured as :obj:`[*B, T, F]` where :obj:`B` are
                the batch size, :obj:`T` the time dimension and :obj:`F` the feature dimension(s).
            params (TensorDictBase, optional): A nested TensorDict containing the params
                to be passed to the functional value network module.
            target_params (TensorDictBase, optional): A nested TensorDict containing the
                target params to be passed to the functional value network module.

        Returns:
            An updated TensorDict with an advantage and a value_error keys as defined in the constructor.
        """
        raise NotImplementedError

    def __init__(
        self,
        *,
        value_network: TensorDictModule,
        differentiable: bool = False,
        advantage_key: Union[str, Tuple] = "advantage",
        value_target_key: Union[str, Tuple] = "value_target",
        value_key: Union[str, Tuple] = "state_value",
        skip_existing: Optional[bool] = None,
    ):
        super().__init__()
        self.differentiable = differentiable
        self.skip_existing = skip_existing
        self.value_network = value_network
        self.value_key = value_key
        if (
            hasattr(value_network, "out_keys")
            and value_key not in value_network.out_keys
        ):
            raise KeyError(
                f"value key '{value_key}' not found in value network out_keys."
            )

        self.advantage_key = advantage_key
        self.value_target_key = value_target_key

        try:
            self.in_keys = (
                value_network.in_keys
                + [("next", "reward"), ("next", "done")]
                + [("next", in_key) for in_key in value_network.in_keys]
            )
        except AttributeError:
            # value network does not have an `in_keys` attribute
            self.in_keys = []
            pass

        self.out_keys = [self.advantage_key, self.value_target_key]

    def value_estimate(
        self,
        tensordict,
        target_params: Optional[TensorDictBase] = None,
        **kwargs,
    ):
        """Gets a value estimate, usually used as a target value for the value network.

        If the state value key is present under ``tensordict.get(("next", self.value_key))``
        then this value will be used without recurring to the value network.

        Args:
            tensordict (TensorDictBase): the tensordict containing the data to
                read.
            target_params (TensorDictBase, optional): A nested TensorDict containing the
                target params to be passed to the functional value network module.
            **kwargs: the keyword arguments to be passed to the value network.

        Returns: a tensor corresponding to the state value.

        """
        raise NotImplementedError

    @property
    def is_functional(self):
        if isinstance(self.value_network, nn.Module):
            return is_functional(self.value_network)
        elif self.value_network is None:
            return None
        else:
            raise RuntimeError("Cannot determine if value network is functional.")

    @property
    def is_stateless(self):
        if not self.is_functional:
            return False
        return self.value_network._is_stateless


class TD0Estimator(ValueEstimatorBase):
    """Temporal Difference (TD(0)) estimate of advantage function.

    AKA bootstrapped temporal difference or 1-step return.

    Keyword Args:
        gamma (scalar): exponential mean discount.
        value_network (TensorDictModule): value operator used to retrieve
            the value estimates.
        average_rewards (bool, optional): if ``True``, rewards will be standardized
            before the TD is computed.
        differentiable (bool, optional): if ``True``, gradients are propagated through
            the computation of the value function. Default is ``False``.

            .. note::
              The proper way to make the function call non-differentiable is to
              decorate it in a `torch.no_grad()` context manager/decorator or
              pass detached parameters for functional modules.

        advantage_key (str or tuple of str, optional): the key of the advantage entry.
            Defaults to "advantage".
        value_target_key (str or tuple of str, optional): the key of the advantage entry.
            Defaults to "value_target".
        value_key (str or tuple of str, optional): the value key to read from the input tensordict.
            Defaults to "state_value".
        skip_existing (bool, optional): if ``True``, the value network will skip
            modules which outputs are already present in the tensordict.
            Defaults to ``None``, ie. the value of :func:`tensordict.nn.skip_existing()`
            is not affected.

    """

    def __init__(
        self,
        *,
        gamma: Union[float, torch.Tensor],
        value_network: TensorDictModule,
        average_rewards: bool = False,
        differentiable: bool = False,
        advantage_key: Union[str, Tuple] = "advantage",
        value_target_key: Union[str, Tuple] = "value_target",
        value_key: Union[str, Tuple] = "state_value",
        skip_existing: Optional[bool] = None,
    ):
        super().__init__(
            value_network=value_network,
            differentiable=differentiable,
            advantage_key=advantage_key,
            value_target_key=value_target_key,
            value_key=value_key,
            skip_existing=skip_existing,
        )
        try:
            device = next(value_network.parameters()).device
        except (AttributeError, StopIteration):
            device = torch.device("cpu")
        self.register_buffer("gamma", torch.tensor(gamma, device=device))
        self.average_rewards = average_rewards

    @_self_set_skip_existing
    @_self_set_grad_enabled
    @dispatch
    def forward(
        self,
        tensordict: TensorDictBase,
        params: Optional[TensorDictBase] = None,
        target_params: Optional[TensorDictBase] = None,
    ) -> TensorDictBase:
        """Computes the TD(0) advantage given the data in tensordict.

        If a functional module is provided, a nested TensorDict containing the parameters
        (and if relevant the target parameters) can be passed to the module.

        Args:
            tensordict (TensorDictBase): A TensorDict containing the data
                (an observation key, "action", ("next", "reward"), ("next", "done") and "next" tensordict state
                as returned by the environment) necessary to compute the value estimates and the TDEstimate.
                The data passed to this module should be structured as :obj:`[*B, T, F]` where :obj:`B` are
                the batch size, :obj:`T` the time dimension and :obj:`F` the feature dimension(s).
            params (TensorDictBase, optional): A nested TensorDict containing the params
                to be passed to the functional value network module.
            target_params (TensorDictBase, optional): A nested TensorDict containing the
                target params to be passed to the functional value network module.

        Returns:
            An updated TensorDict with an advantage and a value_error keys as defined in the constructor.

        Examples:
            >>> from tensordict import TensorDict
            >>> value_net = TensorDictModule(
            ...     nn.Linear(3, 1), in_keys=["obs"], out_keys=["state_value"]
            ... )
            >>> module = TDEstimate(
            ...     gamma=0.98,
            ...     value_network=value_net,
            ... )
            >>> obs, next_obs = torch.randn(2, 1, 10, 3)
            >>> reward = torch.randn(1, 10, 1)
            >>> done = torch.zeros(1, 10, 1, dtype=torch.bool)
            >>> tensordict = TensorDict({"obs": obs, "next": {"obs": next_obs, "done": done, "reward": reward}}, [1, 10])
            >>> _ = module(tensordict)
            >>> assert "advantage" in tensordict.keys()

        The module supports non-tensordict (i.e. unpacked tensordict) inputs too:

        Examples:
            >>> value_net = TensorDictModule(
            ...     nn.Linear(3, 1), in_keys=["obs"], out_keys=["state_value"]
            ... )
            >>> module = TDEstimate(
            ...     gamma=0.98,
            ...     value_network=value_net,
            ... )
            >>> obs, next_obs = torch.randn(2, 1, 10, 3)
            >>> reward = torch.randn(1, 10, 1)
            >>> done = torch.zeros(1, 10, 1, dtype=torch.bool)
            >>> advantage, value_target = module(obs=obs, reward=reward, done=done, next_obs=next_obs)

        """
        if tensordict.batch_dims < 1:
            raise RuntimeError(
                "Expected input tensordict to have at least one dimensions, got"
                f"tensordict.batch_size = {tensordict.batch_size}"
            )

        kwargs = {}
        if self.is_stateless and params is None:
            raise RuntimeError(
                "Expected params to be passed to advantage module but got none."
            )
        if params is not None:
            kwargs["params"] = params.detach()
        with hold_out_net(self.value_network):
            self.value_network(tensordict, **kwargs)
            value = tensordict.get(self.value_key)

        if params is not None and target_params is None:
            target_params = params.detach()
        value_target = self.value_estimate(tensordict, target_params=target_params)
        tensordict.set("advantage", value_target - value)
        tensordict.set("value_target", value_target)
        return tensordict

    def value_estimate(
        self,
        tensordict,
        target_params: Optional[TensorDictBase] = None,
        **kwargs,
    ):
        reward = tensordict.get(("next", "reward"))
        device = reward.device
        gamma = self.gamma.to(device)
        steps_to_next_obs = tensordict.get("steps_to_next_obs", None)
        if steps_to_next_obs is not None:
            gamma = gamma ** steps_to_next_obs.view_as(reward)

        if self.average_rewards:
            reward = reward - reward.mean()
            reward = reward / reward.std().clamp_min(1e-4)
            tensordict.set(
                ("next", "reward"), reward
            )  # we must update the rewards if they are used later in the code
        step_td = step_mdp(tensordict)
        if self.value_network is not None:
            if target_params is not None:
                kwargs["params"] = target_params
            with hold_out_net(self.value_network):
                self.value_network(step_td, **kwargs)
        next_value = step_td.get(self.value_key)

        done = tensordict.get(("next", "done"))
        value_target = td0_return_estimate(
            gamma=gamma, next_state_value=next_value, reward=reward, done=done
        )
        return value_target


class TD1Estimator(ValueEstimatorBase):
    r""":math:`\infty`-Temporal Difference (TD(1)) estimate of advantage function.

    Keyword Args:
        gamma (scalar): exponential mean discount.
        value_network (TensorDictModule): value operator used to retrieve the value estimates.
        average_rewards (bool, optional): if ``True``, rewards will be standardized
            before the TD is computed.
        differentiable (bool, optional): if ``True``, gradients are propagated through
            the computation of the value function. Default is ``False``.

            .. note::
              The proper way to make the function call non-differentiable is to
              decorate it in a `torch.no_grad()` context manager/decorator or
              pass detached parameters for functional modules.

        advantage_key (str or tuple of str, optional): the key of the advantage entry.
            Defaults to "advantage".
        value_target_key (str or tuple of str, optional): the key of the advantage entry.
            Defaults to "value_target".
        value_key (str or tuple of str, optional): the value key to read from the input tensordict.
            Defaults to "state_value".
        skip_existing (bool, optional): if ``True``, the value network will skip
            modules which outputs are already present in the tensordict.
            Defaults to ``None``, ie. the value of :func:`tensordict.nn.skip_existing()`
            is not affected.

    """

    def __init__(
        self,
        *,
        gamma: Union[float, torch.Tensor],
        value_network: TensorDictModule,
        average_rewards: bool = False,
        differentiable: bool = False,
        advantage_key: Union[str, Tuple] = "advantage",
        value_target_key: Union[str, Tuple] = "value_target",
        value_key: Union[str, Tuple] = "state_value",
        skip_existing: Optional[bool] = None,
    ):
        super().__init__(
            value_network=value_network,
            differentiable=differentiable,
            advantage_key=advantage_key,
            value_target_key=value_target_key,
            value_key=value_key,
            skip_existing=skip_existing,
        )
        try:
            device = next(value_network.parameters()).device
        except (AttributeError, StopIteration):
            device = torch.device("cpu")
        self.register_buffer("gamma", torch.tensor(gamma, device=device))
        self.average_rewards = average_rewards

    @_self_set_skip_existing
    @_self_set_grad_enabled
    @dispatch
    def forward(
        self,
        tensordict: TensorDictBase,
        params: Optional[TensorDictBase] = None,
        target_params: Optional[TensorDictBase] = None,
    ) -> TensorDictBase:
        """Computes the TD(1) advantage given the data in tensordict.

        If a functional module is provided, a nested TensorDict containing the parameters
        (and if relevant the target parameters) can be passed to the module.

        Args:
            tensordict (TensorDictBase): A TensorDict containing the data
                (an observation key, "action", ("next", "reward"), ("next", "done") and "next" tensordict state
                as returned by the environment) necessary to compute the value estimates and the TDEstimate.
                The data passed to this module should be structured as :obj:`[*B, T, F]` where :obj:`B` are
                the batch size, :obj:`T` the time dimension and :obj:`F` the feature dimension(s).
            params (TensorDictBase, optional): A nested TensorDict containing the params
                to be passed to the functional value network module.
            target_params (TensorDictBase, optional): A nested TensorDict containing the
                target params to be passed to the functional value network module.

        Returns:
            An updated TensorDict with an advantage and a value_error keys as defined in the constructor.

        Examples:
            >>> from tensordict import TensorDict
            >>> value_net = TensorDictModule(
            ...     nn.Linear(3, 1), in_keys=["obs"], out_keys=["state_value"]
            ... )
            >>> module = TDEstimate(
            ...     gamma=0.98,
            ...     value_network=value_net,
            ... )
            >>> obs, next_obs = torch.randn(2, 1, 10, 3)
            >>> reward = torch.randn(1, 10, 1)
            >>> done = torch.zeros(1, 10, 1, dtype=torch.bool)
            >>> tensordict = TensorDict({"obs": obs, "next": {"obs": next_obs, "done": done, "reward": reward}}, [1, 10])
            >>> _ = module(tensordict)
            >>> assert "advantage" in tensordict.keys()

        The module supports non-tensordict (i.e. unpacked tensordict) inputs too:

        Examples:
            >>> value_net = TensorDictModule(
            ...     nn.Linear(3, 1), in_keys=["obs"], out_keys=["state_value"]
            ... )
            >>> module = TDEstimate(
            ...     gamma=0.98,
            ...     value_network=value_net,
            ... )
            >>> obs, next_obs = torch.randn(2, 1, 10, 3)
            >>> reward = torch.randn(1, 10, 1)
            >>> done = torch.zeros(1, 10, 1, dtype=torch.bool)
            >>> advantage, value_target = module(obs=obs, reward=reward, done=done, next_obs=next_obs)

        """
        if tensordict.batch_dims < 1:
            raise RuntimeError(
                "Expected input tensordict to have at least one dimensions, got"
                f"tensordict.batch_size = {tensordict.batch_size}"
            )

        kwargs = {}
        if self.is_stateless and params is None:
            raise RuntimeError(
                "Expected params to be passed to advantage module but got none."
            )
        if params is not None:
            kwargs["params"] = params.detach()
        if self.value_network is not None:
            with hold_out_net(self.value_network):
                self.value_network(tensordict, **kwargs)
        value = tensordict.get(self.value_key)

        if params is not None and target_params is None:
            target_params = params.detach()
        value_target = self.value_estimate(tensordict, target_params=target_params)
        tensordict.set("advantage", value_target - value)
        tensordict.set("value_target", value_target)
        return tensordict

    def value_estimate(
        self,
        tensordict,
        target_params: Optional[TensorDictBase] = None,
        **kwargs,
    ):
        reward = tensordict.get(("next", "reward"))
        device = reward.device
        gamma = self.gamma.to(device)
        steps_to_next_obs = tensordict.get("steps_to_next_obs", None)
        if steps_to_next_obs is not None:
            gamma = gamma ** steps_to_next_obs.view_as(reward)

        if self.average_rewards:
            reward = reward - reward.mean()
            reward = reward / reward.std().clamp_min(1e-4)
            tensordict.set(
                ("next", "reward"), reward
            )  # we must update the rewards if they are used later in the code
        step_td = step_mdp(tensordict)
        if self.value_network is not None:
            if target_params is not None:
                kwargs["params"] = target_params
            with hold_out_net(self.value_network):
                self.value_network(step_td, **kwargs)
        next_value = step_td.get(self.value_key)

        done = tensordict.get(("next", "done"))
        value_target = vec_td1_return_estimate(
            gamma, next_value, reward, done, time_dim=tensordict.ndim - 1
        )
        return value_target


class TDLambdaEstimator(ValueEstimatorBase):
    r"""TD(:math:`\lambda`) estimate of advantage function.

    Args:
        gamma (scalar): exponential mean discount.
        lmbda (scalar): trajectory discount.
        value_network (TensorDictModule): value operator used to retrieve the value estimates.
        average_rewards (bool, optional): if ``True``, rewards will be standardized
            before the TD is computed.
        differentiable (bool, optional): if ``True``, gradients are propagated through
            the computation of the value function. Default is ``False``.

            .. note::
              The proper way to make the function call non-differentiable is to
              decorate it in a `torch.no_grad()` context manager/decorator or
              pass detached parameters for functional modules.

        vectorized (bool, optional): whether to use the vectorized version of the
            lambda return. Default is `True`.
        advantage_key (str or tuple of str, optional): the key of the advantage entry.
            Defaults to "advantage".
        value_target_key (str or tuple of str, optional): the key of the advantage entry.
            Defaults to "value_target".
        value_key (str or tuple of str, optional): the value key to read from the input tensordict.
            Defaults to "state_value".
        skip_existing (bool, optional): if ``True``, the value network will skip
            modules which outputs are already present in the tensordict.
            Defaults to ``None``, ie. the value of :func:`tensordict.nn.skip_existing()`
            is not affected.

    """

    def __init__(
        self,
        *,
        gamma: Union[float, torch.Tensor],
        lmbda: Union[float, torch.Tensor],
        value_network: TensorDictModule,
        average_rewards: bool = False,
        differentiable: bool = False,
        vectorized: bool = True,
        advantage_key: Union[str, Tuple] = "advantage",
        value_target_key: Union[str, Tuple] = "value_target",
        value_key: Union[str, Tuple] = "state_value",
        skip_existing: Optional[bool] = None,
    ):
        super().__init__(
            value_network=value_network,
            differentiable=differentiable,
            advantage_key=advantage_key,
            value_target_key=value_target_key,
            value_key=value_key,
            skip_existing=skip_existing,
        )
        try:
            device = next(value_network.parameters()).device
        except (AttributeError, StopIteration):
            device = torch.device("cpu")
        self.register_buffer("gamma", torch.tensor(gamma, device=device))
        self.register_buffer("lmbda", torch.tensor(lmbda, device=device))
        self.average_rewards = average_rewards
        self.vectorized = vectorized

    @_self_set_skip_existing
    @_self_set_grad_enabled
    @dispatch
    def forward(
        self,
        tensordict: TensorDictBase,
        params: Optional[List[Tensor]] = None,
        target_params: Optional[List[Tensor]] = None,
    ) -> TensorDictBase:
        r"""Computes the TD(:math:`\lambda`) advantage given the data in tensordict.

        If a functional module is provided, a nested TensorDict containing the parameters
        (and if relevant the target parameters) can be passed to the module.

        Args:
            tensordict (TensorDictBase): A TensorDict containing the data
                (an observation key, "action", ("next", "reward"), ("next", "done") and "next" tensordict state
                as returned by the environment) necessary to compute the value estimates and the TDLambdaEstimate.
                The data passed to this module should be structured as :obj:`[*B, T, F]` where :obj:`B` are
                the batch size, :obj:`T` the time dimension and :obj:`F` the feature dimension(s).
            params (TensorDictBase, optional): A nested TensorDict containing the params
                to be passed to the functional value network module.
            target_params (TensorDictBase, optional): A nested TensorDict containing the
                target params to be passed to the functional value network module.

        Returns:
            An updated TensorDict with an advantage and a value_error keys as defined in the constructor.

        Examples:
            >>> from tensordict import TensorDict
            >>> value_net = TensorDictModule(
            ...     nn.Linear(3, 1), in_keys=["obs"], out_keys=["state_value"]
            ... )
            >>> module = TDLambdaEstimator(
            ...     gamma=0.98,
            ...     lmbda=0.94,
            ...     value_network=value_net,
            ... )
            >>> obs, next_obs = torch.randn(2, 1, 10, 3)
            >>> reward = torch.randn(1, 10, 1)
            >>> done = torch.zeros(1, 10, 1, dtype=torch.bool)
            >>> tensordict = TensorDict({"obs": obs, "next": {"obs": next_obs, "done": done, "reward": reward}}, [1, 10])
            >>> _ = module(tensordict)
            >>> assert "advantage" in tensordict.keys()

        The module supports non-tensordict (i.e. unpacked tensordict) inputs too:

        Examples:
            >>> value_net = TensorDictModule(
            ...     nn.Linear(3, 1), in_keys=["obs"], out_keys=["state_value"]
            ... )
            >>> module = TDLambdaEstimator(
            ...     gamma=0.98,
            ...     lmbda=0.94,
            ...     value_network=value_net,
            ... )
            >>> obs, next_obs = torch.randn(2, 1, 10, 3)
            >>> reward = torch.randn(1, 10, 1)
            >>> done = torch.zeros(1, 10, 1, dtype=torch.bool)
            >>> advantage, value_target = module(obs=obs, reward=reward, done=done, next_obs=next_obs)

        """
        if tensordict.batch_dims < 1:
            raise RuntimeError(
                "Expected input tensordict to have at least one dimensions, got"
                f"tensordict.batch_size = {tensordict.batch_size}"
            )
        kwargs = {}
        if self.is_stateless and params is None:
            raise RuntimeError(
                "Expected params to be passed to advantage module but got none."
            )
        if params is not None:
            kwargs["params"] = params
        if self.value_network is not None:
            with hold_out_net(self.value_network):
                self.value_network(tensordict, **kwargs)
        value = tensordict.get(self.value_key)
        if params is not None and target_params is None:
            target_params = params.detach()
        value_target = self.value_estimate(tensordict, target_params=target_params)

        tensordict.set(self.advantage_key, value_target - value)
        tensordict.set(self.value_target_key, value_target)
        return tensordict

    def value_estimate(
        self,
        tensordict,
        target_params: Optional[TensorDictBase] = None,
        **kwargs,
    ):
        reward = tensordict.get(("next", "reward"))
        device = reward.device
        gamma = self.gamma.to(device)
        steps_to_next_obs = tensordict.get("steps_to_next_obs", None)
        if steps_to_next_obs is not None:
            gamma = gamma ** steps_to_next_obs.view_as(reward)

        lmbda = self.lmbda
        if self.average_rewards:
            reward = reward - reward.mean()
            reward = reward / reward.std().clamp_min(1e-4)
            tensordict.set(
                ("next", "reward"), reward
            )  # we must update the rewards if they are used later in the code

        step_td = step_mdp(tensordict)
        if self.value_network is not None:
            if target_params is not None:
                kwargs["params"] = target_params
            with hold_out_net(self.value_network):
                self.value_network(step_td, **kwargs)
        next_value = step_td.get(self.value_key)

        done = tensordict.get(("next", "done"))
        if self.vectorized:
            val = vec_td_lambda_return_estimate(
                gamma, lmbda, next_value, reward, done, time_dim=tensordict.ndim - 1
            )
        else:
            val = td_lambda_return_estimate(
                gamma, lmbda, next_value, reward, done, time_dim=tensordict.ndim - 1
            )
        return val


class GAE(ValueEstimatorBase):
    """A class wrapper around the generalized advantage estimate functional.

    Refer to "HIGH-DIMENSIONAL CONTINUOUS CONTROL USING GENERALIZED ADVANTAGE ESTIMATION"
    https://arxiv.org/pdf/1506.02438.pdf for more context.

    Args:
        gamma (scalar): exponential mean discount.
        lmbda (scalar): trajectory discount.
        value_network (TensorDictModule): value operator used to retrieve the value estimates.
        average_gae (bool): if ``True``, the resulting GAE values will be standardized.
            Default is ``False``.
        differentiable (bool, optional): if ``True``, gradients are propagated through
            the computation of the value function. Default is ``False``.

            .. note::
              The proper way to make the function call non-differentiable is to
              decorate it in a `torch.no_grad()` context manager/decorator or
              pass detached parameters for functional modules.

        vectorized (bool, optional): whether to use the vectorized version of the
            lambda return. Default is `True`.
        advantage_key (str or tuple of str, optional): the key of the advantage entry.
            Defaults to "advantage".
        value_target_key (str or tuple of str, optional): the key of the advantage entry.
            Defaults to "value_target".
        value_key (str or tuple of str, optional): the value key to read from the input tensordict.
            Defaults to "state_value".
        skip_existing (bool, optional): if ``True``, the value network will skip
            modules which outputs are already present in the tensordict.
            Defaults to ``None``, ie. the value of :func:`tensordict.nn.skip_existing()`
            is not affected.

    GAE will return an :obj:`"advantage"` entry containing the advange value. It will also
    return a :obj:`"value_target"` entry with the return value that is to be used
    to train the value network. Finally, if :obj:`gradient_mode` is :obj:`True`,
    an additional and differentiable :obj:`"value_error"` entry will be returned,
    which simple represents the difference between the return and the value network
    output (i.e. an additional distance loss should be applied to that signed value).

    .. note::
      As other advantage functions do, if the ``value_key`` is already present
      in the input tensordict, the GAE module will ignore the calls to the value
      network (if any) and use the provided value instead.

    """

    def __init__(
        self,
        *,
        gamma: Union[float, torch.Tensor],
        lmbda: float,
        value_network: TensorDictModule,
        average_gae: bool = False,
        differentiable: bool = False,
        vectorized: bool = True,
        advantage_key: Union[str, Tuple] = "advantage",
        value_target_key: Union[str, Tuple] = "value_target",
        value_key: Union[str, Tuple] = "state_value",
        skip_existing: Optional[bool] = None,
    ):
        super().__init__(
            value_network=value_network,
            differentiable=differentiable,
            advantage_key=advantage_key,
            value_target_key=value_target_key,
            value_key=value_key,
            skip_existing=skip_existing,
        )
        try:
            device = next(value_network.parameters()).device
        except (AttributeError, StopIteration):
            device = torch.device("cpu")
        self.register_buffer("gamma", torch.tensor(gamma, device=device))
        self.register_buffer("lmbda", torch.tensor(lmbda, device=device))
        self.average_gae = average_gae
        self.vectorized = vectorized

    @_self_set_skip_existing
    @_self_set_grad_enabled
    @dispatch
    def forward(
        self,
        tensordict: TensorDictBase,
        *unused_args,
        params: Optional[List[Tensor]] = None,
        target_params: Optional[List[Tensor]] = None,
    ) -> TensorDictBase:
        """Computes the GAE given the data in tensordict.

        If a functional module is provided, a nested TensorDict containing the parameters
        (and if relevant the target parameters) can be passed to the module.

        Args:
            tensordict (TensorDictBase): A TensorDict containing the data
                (an observation key, "action", "reward", "done" and "next" tensordict state
                as returned by the environment) necessary to compute the value estimates and the GAE.
                The data passed to this module should be structured as :obj:`[*B, T, F]` where :obj:`B` are
                the batch size, :obj:`T` the time dimension and :obj:`F` the feature dimension(s).
            params (TensorDictBase, optional): A nested TensorDict containing the params
                to be passed to the functional value network module.
            target_params (TensorDictBase, optional): A nested TensorDict containing the
                target params to be passed to the functional value network module.

        Returns:
            An updated TensorDict with an advantage and a value_error keys as defined in the constructor.

        Examples:
            >>> from tensordict import TensorDict
            >>> value_net = TensorDictModule(
            ...     nn.Linear(3, 1), in_keys=["obs"], out_keys=["state_value"]
            ... )
            >>> module = GAE(
            ...     gamma=0.98,
            ...     lmbda=0.94,
            ...     value_network=value_net,
            ...     differentiable=False,
            ... )
            >>> obs, next_obs = torch.randn(2, 1, 10, 3)
            >>> reward = torch.randn(1, 10, 1)
            >>> done = torch.zeros(1, 10, 1, dtype=torch.bool)
            >>> tensordict = TensorDict({"obs": obs, "next": {"obs": next_obs}, "done": done, "reward": reward}, [1, 10])
            >>> _ = module(tensordict)
            >>> assert "advantage" in tensordict.keys()

        The module supports non-tensordict (i.e. unpacked tensordict) inputs too:

        Examples:
            >>> value_net = TensorDictModule(
            ...     nn.Linear(3, 1), in_keys=["obs"], out_keys=["state_value"]
            ... )
            >>> module = GAE(
            ...     gamma=0.98,
            ...     lmbda=0.94,
            ...     value_network=value_net,
            ...     differentiable=False,
            ... )
            >>> obs, next_obs = torch.randn(2, 1, 10, 3)
            >>> reward = torch.randn(1, 10, 1)
            >>> done = torch.zeros(1, 10, 1, dtype=torch.bool)
            >>> advantage, value_target = module(obs=obs, reward=reward, done=done, next_obs=next_obs)

        """
        if tensordict.batch_dims < 1:
            raise RuntimeError(
                "Expected input tensordict to have at least one dimensions, got "
                f"tensordict.batch_size = {tensordict.batch_size}"
            )
        reward = tensordict.get(("next", "reward"))
        device = reward.device
        gamma, lmbda = self.gamma.to(device), self.lmbda.to(device)
        steps_to_next_obs = tensordict.get("steps_to_next_obs", None)
        if steps_to_next_obs is not None:
            gamma = gamma ** steps_to_next_obs.view_as(reward)

        kwargs = {}
        if self.is_stateless and params is None:
            raise RuntimeError(
                "Expected params to be passed to advantage module but got none."
            )
        if params is not None:
            kwargs["params"] = params

        if self.value_network is not None:
            with hold_out_net(self.value_network):
                # we may still need to pass gradient, but we don't want to assign grads to
                # value net params
                self.value_network(tensordict, **kwargs)

        value = tensordict.get(self.value_key)

        step_td = step_mdp(tensordict)
        if target_params is not None:
            # we assume that target parameters are not differentiable
            kwargs["params"] = target_params
        elif "params" in kwargs:
            kwargs["params"] = kwargs["params"].detach()
        if self.value_network is not None:
            with hold_out_net(self.value_network):
                # we may still need to pass gradient, but we don't want to assign grads to
                # value net params
                self.value_network(step_td, **kwargs)
        next_value = step_td.get(self.value_key)
        done = tensordict.get(("next", "done"))
        if self.vectorized:
            adv, value_target = vec_generalized_advantage_estimate(
                gamma,
                lmbda,
                value,
                next_value,
                reward,
                done,
                time_dim=tensordict.ndim - 1,
            )
        else:
            adv, value_target = generalized_advantage_estimate(
                gamma,
                lmbda,
                value,
                next_value,
                reward,
                done,
                time_dim=tensordict.ndim - 1,
            )

        if self.average_gae:
            loc = adv.mean()
            scale = adv.std().clamp_min(1e-4)
            adv = adv - loc
            adv = adv / scale

        tensordict.set(self.advantage_key, adv)
        tensordict.set(self.value_target_key, value_target)

        return tensordict

    def value_estimate(
        self,
        tensordict,
        params: Optional[TensorDictBase] = None,
        target_params: Optional[TensorDictBase] = None,
        **kwargs,
    ):
        if tensordict.batch_dims < 1:
            raise RuntimeError(
                "Expected input tensordict to have at least one dimensions, got"
                f"tensordict.batch_size = {tensordict.batch_size}"
            )
        reward = tensordict.get(("next", "reward"))
        device = reward.device
        gamma, lmbda = self.gamma.to(device), self.lmbda.to(device)
        steps_to_next_obs = tensordict.get("steps_to_next_obs", None)
        if steps_to_next_obs is not None:
            gamma = gamma ** steps_to_next_obs.view_as(reward)

        if self.is_stateless and params is None:
            raise RuntimeError(
                "Expected params to be passed to advantage module but got none."
            )
        if params is not None:
            kwargs["params"] = params
        if self.value_network is not None:
            with hold_out_net(self.value_network):
                # we may still need to pass gradient, but we don't want to assign grads to
                # value net params
                self.value_network(tensordict, **kwargs)

        value = tensordict.get(self.value_key)

        step_td = step_mdp(tensordict)
        if target_params is not None:
            # we assume that target parameters are not differentiable
            kwargs["params"] = target_params
        elif "params" in kwargs:
            kwargs["params"] = kwargs["params"].detach()
        if self.value_network is not None:
            with hold_out_net(self.value_network):
                # we may still need to pass gradient, but we don't want to assign grads to
                # value net params
                self.value_network(step_td, **kwargs)
        next_value = step_td.get(self.value_key)
        done = tensordict.get(("next", "done"))
        _, value_target = vec_generalized_advantage_estimate(
            gamma, lmbda, value, next_value, reward, done, time_dim=tensordict.ndim - 1
        )
        return value_target


def _deprecate_class(cls, new_cls):
    @wraps(cls.__init__)
    def new_init(self, *args, **kwargs):
        warnings.warn(f"class {cls} is deprecated, please use {new_cls} instead.")
        cls.__init__(self, *args, **kwargs)

    cls.__init__ = new_init


TD0Estimate = type("TD0Estimate", TD0Estimator.__bases__, dict(TD0Estimator.__dict__))
_deprecate_class(TD0Estimate, TD0Estimator)
TD1Estimate = type("TD1Estimate", TD1Estimator.__bases__, dict(TD1Estimator.__dict__))
_deprecate_class(TD1Estimate, TD1Estimator)
TDLambdaEstimate = type(
    "TDLambdaEstimate", TDLambdaEstimator.__bases__, dict(TDLambdaEstimator.__dict__)
)
_deprecate_class(TDLambdaEstimate, TDLambdaEstimator)
