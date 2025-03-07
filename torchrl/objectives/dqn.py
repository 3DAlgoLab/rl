# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import warnings
from typing import Union

import torch
from tensordict import TensorDict, TensorDictBase
from torch import nn
from torchrl.data.tensor_specs import TensorSpec

from torchrl.envs.utils import step_mdp
from torchrl.modules.tensordict_module.actors import (
    DistributionalQValueActor,
    QValueActor,
)
from torchrl.modules.tensordict_module.common import ensure_tensordict_compatible

from ..modules.utils.utils import _find_action_space

from .common import LossModule
from .utils import (
    _GAMMA_LMBDA_DEPREC_WARNING,
    default_value_kwargs,
    distance_loss,
    ValueEstimators,
)
from .value import TDLambdaEstimator
from .value.advantages import TD0Estimator, TD1Estimator


class DQNLoss(LossModule):
    """The DQN Loss class.

    Args:
        value_network (QValueActor or nn.Module): a Q value operator.

    Keyword Args:
        loss_function (str): loss function for the value discrepancy. Can be one of "l1", "l2" or "smooth_l1".
        priority_key (str, optional): the key at which priority is assumed to
            be stored within TensorDicts added to this ReplayBuffer.
            This is to be used when the sampler is of type
            :class:`~torchrl.data.PrioritizedSampler`.
            Defaults to ``"td_error"``.
        delay_value (bool, optional): whether to duplicate the value network
            into a new target value network to
            create a double DQN. Default is ``False``.
        action_space (str or TensorSpec, optional): Action space. Must be one of
            ``"one-hot"``, ``"mult_one_hot"``, ``"binary"`` or ``"categorical"``,
            or an instance of the corresponding specs (:class:`torchrl.data.OneHotDiscreteTensorSpec`,
            :class:`torchrl.data.MultiOneHotDiscreteTensorSpec`,
            :class:`torchrl.data.BinaryDiscreteTensorSpec` or :class:`torchrl.data.DiscreteTensorSpec`).
            If not provided, an attempt to retrieve it from the value network
            will be made.

    """

    default_value_estimator = ValueEstimators.TDLambda

    def __init__(
        self,
        value_network: Union[QValueActor, nn.Module],
        *,
        loss_function: str = "l2",
        priority_key: str = "td_error",
        delay_value: bool = False,
        gamma: float = None,
        action_space: Union[str, TensorSpec] = None,
    ) -> None:

        super().__init__()
        self.delay_value = delay_value
        value_network = ensure_tensordict_compatible(
            module=value_network,
            wrapper_type=QValueActor,
            action_space=action_space,
        )

        self.convert_to_functional(
            value_network,
            "value_network",
            create_target_params=self.delay_value,
        )

        self.value_network_in_keys = value_network.in_keys

        self.loss_function = loss_function
        self.priority_key = priority_key
        if action_space is None:
            # infer from value net
            try:
                action_space = value_network.spec
            except AttributeError:
                # let's try with action_space then
                try:
                    action_space = value_network.action_space
                except AttributeError:
                    raise ValueError(self.ACTION_SPEC_ERROR)
        if action_space is None:
            warnings.warn(
                "action_space was not specified. DQNLoss will default to 'one-hot'."
                "This behaviour will be deprecated soon and a space will have to be passed."
                "Check the DQNLoss documentation to see how to pass the action space. "
            )
            action_space = "one-hot"
        self.action_space = _find_action_space(action_space)

        if gamma is not None:
            warnings.warn(_GAMMA_LMBDA_DEPREC_WARNING, category=DeprecationWarning)
            self.gamma = gamma

    def make_value_estimator(self, value_type: ValueEstimators = None, **hyperparams):
        if value_type is None:
            value_type = self.default_value_estimator
        self.value_type = value_type
        hp = dict(default_value_kwargs(value_type))
        if hasattr(self, "gamma"):
            hp["gamma"] = self.gamma
        hp.update(hyperparams)
        if value_type is ValueEstimators.TD1:
            self._value_estimator = TD1Estimator(
                **hp,
                value_network=self.value_network,
                advantage_key="advantage",
                value_target_key="value_target",
                value_key="chosen_action_value",
            )
        elif value_type is ValueEstimators.TD0:
            self._value_estimator = TD0Estimator(
                **hp,
                value_network=self.value_network,
                advantage_key="advantage",
                value_target_key="value_target",
                value_key="chosen_action_value",
            )
        elif value_type is ValueEstimators.GAE:
            raise NotImplementedError(
                f"Value type {value_type} it not implemented for loss {type(self)}."
            )
        elif value_type is ValueEstimators.TDLambda:
            self._value_estimator = TDLambdaEstimator(
                **hp,
                value_network=self.value_network,
                advantage_key="advantage",
                value_target_key="value_target",
                value_key="chosen_action_value",
            )
        else:
            raise NotImplementedError(f"Unknown value type {value_type}")

    def forward(self, input_tensordict: TensorDictBase) -> TensorDict:
        """Computes the DQN loss given a tensordict sampled from the replay buffer.

        This function will also write a "td_error" key that can be used by prioritized replay buffers to assign
            a priority to items in the tensordict.

        Args:
            input_tensordict (TensorDictBase): a tensordict with keys ["action"] and the in_keys of
                the value network (observations, "done", "reward" in a "next" tensordict).

        Returns:
            a tensor containing the DQN loss.

        """
        device = self.device if self.device is not None else input_tensordict.device
        tensordict = input_tensordict.to(device)
        if tensordict.device != device:
            raise RuntimeError(
                f"device {device} was expected for "
                f"{tensordict.__class__.__name__} but {tensordict.device} was found"
            )

        for k, t in tensordict.items():
            if t.device != device:
                raise RuntimeError(
                    f"found key value pair {k}-{t.shape} "
                    f"with device {t.device} when {device} was required"
                )

        td_copy = tensordict.clone()
        if td_copy.device != tensordict.device:
            raise RuntimeError(f"{tensordict} and {td_copy} have different devices")
        assert hasattr(self.value_network, "_is_stateless")
        self.value_network(
            td_copy,
            params=self.value_network_params,
        )

        action = tensordict.get("action")
        pred_val = td_copy.get("action_value")

        if self.action_space == "categorical":
            if action.shape != pred_val.shape:
                # unsqueeze the action if it lacks on trailing singleton dim
                action = action.unsqueeze(-1)
            pred_val_index = torch.gather(pred_val, -1, index=action).squeeze(-1)
        else:
            action = action.to(torch.float)
            pred_val_index = (pred_val * action).sum(-1)

        target_value = self.value_estimator.value_estimate(
            tensordict.clone(False), target_params=self.target_value_network_params
        ).squeeze(-1)

        priority_tensor = (pred_val_index - target_value).pow(2)
        priority_tensor = priority_tensor.detach().unsqueeze(-1)
        if input_tensordict.device is not None:
            priority_tensor = priority_tensor.to(input_tensordict.device)

        input_tensordict.set(
            self.priority_key,
            priority_tensor,
            inplace=True,
        )
        loss = distance_loss(pred_val_index, target_value, self.loss_function)
        return TensorDict({"loss": loss.mean()}, [])


class DistributionalDQNLoss(LossModule):
    """A distributional DQN loss class.

    Distributional DQN uses a value network that outputs a distribution of
    values over a discrete support of discounted returns (unlike regular DQN
    where the value network outputs a single point prediction of the
    disctounted return).

    For more details regarding Distributional DQN, refer to "A Distributional
    Perspective on Reinforcement Learning",
    https://arxiv.org/pdf/1707.06887.pdf

    Args:
        value_network (DistributionalQValueActor or nn.Module): the distributional Q
            value operator.
        gamma (scalar): a discount factor for return computation.

            .. note::
              Unlike :class:`DQNLoss`, this class does not currently support
              custom value functions. The next value estimation is always
              bootstrapped.

        delay_value (bool): whether to duplicate the value network into a new
            target value network to create double DQN
    """

    def __init__(
        self,
        value_network: Union[DistributionalQValueActor, nn.Module],
        gamma: float,
        priority_key: str = "td_error",
        delay_value: bool = False,
    ):
        super().__init__()
        self.register_buffer("gamma", torch.tensor(gamma))
        self.priority_key = priority_key
        self.delay_value = delay_value

        value_network = ensure_tensordict_compatible(
            module=value_network, wrapper_type=DistributionalQValueActor
        )

        self.convert_to_functional(
            value_network,
            "value_network",
            create_target_params=self.delay_value,
        )
        self.action_space = self.value_network.action_space

    @staticmethod
    def _log_ps_a_default(action, action_log_softmax, batch_size, atoms):
        action_expand = action.unsqueeze(-2).expand_as(action_log_softmax)
        log_ps_a = action_log_softmax.masked_select(action_expand.to(torch.bool))
        log_ps_a = log_ps_a.view(batch_size, atoms)  # log p(s_t, a_t; θonline)
        return log_ps_a

    @staticmethod
    def _log_ps_a_categorical(action, action_log_softmax):
        # Reshaping action of shape `[*batch_sizes, 1]` to `[*batch_sizes, atoms, 1]` for gather.
        if action.shape[-1] != 1:
            action = action.unsqueeze(-1)
        action = action.unsqueeze(-2)
        new_shape = [-1] * len(action.shape)
        new_shape[-2] = action_log_softmax.shape[-2]  # calculating atoms
        action = action.expand(new_shape)
        return torch.gather(action_log_softmax, -1, index=action).squeeze(-1)

    def forward(self, input_tensordict: TensorDictBase) -> TensorDict:
        # from https://github.com/Kaixhin/Rainbow/blob/9ff5567ad1234ae0ed30d8471e8f13ae07119395/agent.py
        device = self.device
        tensordict = TensorDict(
            source=input_tensordict, batch_size=input_tensordict.batch_size
        ).to(device)

        if tensordict.batch_dims != 1:
            raise RuntimeError(
                f"{self.__class__.__name___} expects a 1-dimensional "
                "tensordict as input"
            )
        batch_size = tensordict.batch_size[0]
        support = self.value_network_params["support"]
        atoms = support.numel()
        Vmin = support.min().item()
        Vmax = support.max().item()
        delta_z = (Vmax - Vmin) / (atoms - 1)

        action = tensordict.get("action")
        reward = tensordict.get(("next", "reward"))
        done = tensordict.get(("next", "done"))

        steps_to_next_obs = tensordict.get("steps_to_next_obs", 1)
        discount = self.gamma**steps_to_next_obs

        # Calculate current state probabilities (online network noise already
        # sampled)
        td_clone = tensordict.clone()
        self.value_network(
            td_clone,
            params=self.value_network_params,
        )  # Log probabilities log p(s_t, ·; θonline)
        action_log_softmax = td_clone.get("action_value")

        if self.action_space == "categorical":
            log_ps_a = self._log_ps_a_categorical(action, action_log_softmax)
        else:
            log_ps_a = self._log_ps_a_default(
                action, action_log_softmax, batch_size, atoms
            )

        with torch.no_grad():
            # Calculate nth next state probabilities
            next_td = step_mdp(tensordict)
            self.value_network(
                next_td,
                params=self.value_network_params,
            )  # Probabilities p(s_t+n, ·; θonline)

            next_td_action = next_td.get("action")
            if self.action_space == "categorical":
                argmax_indices_ns = next_td_action.squeeze(-1)
            else:
                argmax_indices_ns = next_td_action.argmax(-1)  # one-hot encoding

            self.value_network(
                next_td,
                params=self.target_value_network_params,
            )  # Probabilities p(s_t+n, ·; θtarget)
            pns = next_td.get("action_value").exp()
            # Double-Q probabilities
            # p(s_t+n, argmax_a[(z, p(s_t+n, a; θonline))]; θtarget)
            pns_a = pns[range(batch_size), :, argmax_indices_ns]

            # Compute Tz (Bellman operator T applied to z)
            # Tz = R^n + (γ^n)z (accounting for terminal states)
            if isinstance(discount, torch.Tensor):
                discount = discount.to("cpu")
            done = done.to("cpu")
            reward = reward.to("cpu")
            support = support.to("cpu")
            pns_a = pns_a.to("cpu")

            Tz = reward + (1 - done.to(reward.dtype)) * discount * support
            if Tz.shape != torch.Size([batch_size, atoms]):
                raise RuntimeError(
                    "Tz shape must be torch.Size([batch_size, atoms]), "
                    f"got Tz.shape={Tz.shape} and batch_size={batch_size}, "
                    f"atoms={atoms}"
                )
            # Clamp between supported values
            Tz = Tz.clamp_(min=Vmin, max=Vmax)
            if not torch.isfinite(Tz).all():
                raise RuntimeError("Tz has some non-finite elements")
            # Compute L2 projection of Tz onto fixed support z
            b = (Tz - Vmin) / delta_z  # b = (Tz - Vmin) / Δz
            low, up = b.floor().to(torch.int64), b.ceil().to(torch.int64)
            # Fix disappearing probability mass when l = b = u (b is int)
            low[(up > 0) & (low == up)] -= 1
            up[(low < (atoms - 1)) & (low == up)] += 1

            # Distribute probability of Tz
            m = torch.zeros(batch_size, atoms)
            offset = torch.linspace(
                0,
                ((batch_size - 1) * atoms),
                batch_size,
                dtype=torch.int64,
                # device=device,
            )
            offset = offset.unsqueeze(1).expand(batch_size, atoms)
            index = (low + offset).view(-1)
            tensor = (pns_a * (up.float() - b)).view(-1)
            # m_l = m_l + p(s_t+n, a*)(u - b)
            m.view(-1).index_add_(0, index, tensor)
            index = (up + offset).view(-1)
            tensor = (pns_a * (b - low.float())).view(-1)
            # m_u = m_u + p(s_t+n, a*)(b - l)
            m.view(-1).index_add_(0, index, tensor)

        # Cross-entropy loss (minimises DKL(m||p(s_t, a_t)))
        loss = -torch.sum(m.to(device) * log_ps_a, 1)
        input_tensordict.set(
            self.priority_key,
            loss.detach().unsqueeze(1).to(input_tensordict.device),
            inplace=True,
        )
        loss_td = TensorDict({"loss": loss.mean()}, [])
        return loss_td

    def make_value_estimator(self, value_type: ValueEstimators = None, **hyperparams):
        if value_type is None:
            value_type = self.default_value_estimator
        self.value_type = value_type
        if value_type is ValueEstimators.TD1:
            raise NotImplementedError(
                f"value type {value_type} is not implemented for {self.__class__.__name__}."
            )
        elif value_type is ValueEstimators.TD0:
            # see forward call
            pass
        elif value_type is ValueEstimators.GAE:
            raise NotImplementedError(
                f"value type {value_type} is not implemented for {self.__class__.__name__}."
            )
        elif value_type is ValueEstimators.TDLambda:
            raise NotImplementedError(
                f"value type {value_type} is not implemented for {self.__class__.__name__}."
            )
        else:
            raise NotImplementedError(f"Unknown value type {value_type}")

    def _default_value_estimator(self):
        self.make_value_estimator(ValueEstimators.TD0)
