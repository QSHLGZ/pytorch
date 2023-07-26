import contextlib
import functools
from dataclasses import asdict, dataclass, field
from itertools import chain
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple, Union

import torch
import torch.nn as nn
from torch.distributed._tensor import DTensor
from torch.distributed.fsdp import (
    FullOptimStateDictConfig,
    FullStateDictConfig,
    FullyShardedDataParallel as FSDP,
    ShardedOptimStateDictConfig,
    ShardedStateDictConfig,
    StateDictType,
)
from torch.distributed.fsdp._common_utils import (
    _get_module_fsdp_state_if_fully_sharded_module,
    FSDP_WRAPPED_MODULE,
)
from torch.nn.parallel import DistributedDataParallel as DDP


FLAT_PARAM = "_flat_param"
PG = "param_groups"
FQNS_T = Set[str]

_patched_state_dict: Set[Callable] = set()


@dataclass
class DistributedStateDictOptions:
    use_dtensor: bool = True
    # The default should be sharded_state_dict
    fsdp_state_dict_type: StateDictType = StateDictType.SHARDED_STATE_DICT
    save_to_cpu: bool = True
    # Whether to save the frozen parameters. The default is True.
    save_frozen_params: bool = True


@dataclass
class _StateDictInfo(DistributedStateDictOptions):
    fqn_param_mapping: Dict[
        Union[FQNS_T, torch.Tensor], Union[FQNS_T, torch.Tensor]
    ] = field(default_factory=dict)
    handle_model: bool = True
    handle_optim: bool = True
    fsdp_context: Callable = contextlib.nullcontext
    fsdp_modules: List[nn.Module] = field(default_factory=list)


def _get_fqns(model: nn.Module, name: str, skip_ddp_prefix: bool = True) -> FQNS_T:
    """
    This API is used to convert a name of a parameter to the FQNs. The type of
    the returned FQNs is a set of string. For FSDP case, a FlatParameter may
    contain multiple original parameters, hence multiple FQNs.

    Args:
        module (nn.Module): the root model.
        name (str): the name
        skip_ddp_prefix (bool): whether to skip DDP's `module` prefix

    Returns:
        The canonical FQNs based on the model traversal.
    """
    if "." not in name:
        return set([name])

    obj_names = name.split(".")
    fqn_obj_names = []
    curr_obj = model
    for i, curr_obj_name in enumerate(obj_names):
        if isinstance(curr_obj, DDP):
            assert curr_obj_name == "module"
            curr_obj = curr_obj.module
            if not skip_ddp_prefix:
                fqn_obj_names.append(curr_obj_name)
        elif isinstance(curr_obj, FSDP):
            if obj_names[i + 1] == FLAT_PARAM:
                prefix = ".".join(fqn_obj_names)
                flat_param = getattr(curr_obj, FLAT_PARAM)
                if prefix:
                    prefix = f"{prefix}."
                return set(f"{prefix}{fqn}" for fqn in flat_param._fqns)
            curr_obj = getattr(curr_obj, FSDP_WRAPPED_MODULE)
            if curr_obj_name != FSDP_WRAPPED_MODULE:
                fqn_obj_names.append(curr_obj_name)
                curr_obj = getattr(curr_obj, curr_obj_name)
        else:
            fqn_obj_names.append(curr_obj_name)
            curr_obj = getattr(curr_obj, curr_obj_name)

    return set([".".join(fqn_obj_names)])


def _verify_options(
    model: nn.Module,
    optims: Tuple[torch.optim.Optimizer],
    model_only: bool,
    optim_only: bool,
    options: Optional[DistributedStateDictOptions] = None,
) -> _StateDictInfo:
    """
    Verify the model and options passed by the user and generates _StateDictInfo.
    """
    if options is None:
        options = DistributedStateDictOptions()

    fqn_param_mapping: Dict[
        Union[str, torch.Tensor], Union[Set[str], torch.Tensor]
    ] = {}
    for name, param in model.named_parameters():
        fqns = _get_fqns(model, name)
        fqn_param_mapping[param] = fqns
        for fqn in fqns:
            fqn_param_mapping[fqn] = param
        if isinstance(param, DTensor) and not options.use_dtensor:
            # TODO: better way to detect TP.
            raise RuntimeError("TP is used by but use_dtensor is set to False")

    fsdp_modules = FSDP.fsdp_modules(model)
    if fsdp_modules:
        # FSDP API only work if at least one FSDP instance exists.
        if options.fsdp_state_dict_type == StateDictType.FULL_STATE_DICT:
            state_dict_config = FullStateDictConfig(
                offload_to_cpu=True, rank0_only=True
            )
            optim_state_dict_config = FullOptimStateDictConfig(
                offload_to_cpu=True, rank0_only=True
            )
        elif options.fsdp_state_dict_type == StateDictType.SHARDED_STATE_DICT:
            state_dict_config = ShardedStateDictConfig(use_dtensor=options.use_dtensor)
            optim_state_dict_config = ShardedOptimStateDictConfig(
                use_dtensor=options.use_dtensor
            )
        else:
            raise RuntimeError(
                "distributed_state_dict currently support only FSDP "
                "FULL_STATE_DICT and SHARDED_STATE_DICT"
            )
        fsdp_context = functools.partial(
            FSDP.state_dict_type,
            module=model,
            state_dict_type=options.fsdp_state_dict_type,
            state_dict_config=state_dict_config,
            optim_state_dict_config=optim_state_dict_config,
        )
    else:
        fsdp_context = contextlib.nullcontext

    info = _StateDictInfo(
        **asdict(options),
        fqn_param_mapping=fqn_param_mapping,
        fsdp_context=fsdp_context,
        fsdp_modules=fsdp_modules,
    )

    if model_only and optim_only:
        raise RuntimeError(
            "Both model_only and optim_only are set, which one do you need?"
        )
    if model_only and optims:
        raise RuntimeError(
            "If model_only is True optims must be an empty iterable object."
        )
    if optim_only and not optims:
        raise RuntimeError(
            "Optimizers are not passed in but optim_only is set to True."
        )

    info.handle_model = model_only or not optim_only
    info.handle_optim = optim_only or (not model_only and optims)
    return info


def _verify_state_dict(
    model_state_dict: Dict[str, Any],
    optim_state_dict: Dict[str, Any],
    info: _StateDictInfo,
) -> None:

    # FSDP root must exist otherwise FSDP state_dict will be incorrect.
    has_fsdp_root = False
    for module in info.fsdp_modules:
        fsdp_state = _get_module_fsdp_state_if_fully_sharded_module(module)
        if fsdp_state._is_root:
            has_fsdp_root = True
            break
    if info.fsdp_modules and not has_fsdp_root:
        raise RuntimeError("The model has FSDP modules but no FSDP root module exists.")

    # Verify if the model_state_dict and optim_state_dict are valid. This API
    # should give the users an explicit error message to debug or report.
    if info.handle_model and not model_state_dict:
        raise RuntimeError(
            "The option indicates that model state_dict is required to save "
            "or load, but model state_dict is empty."
        )

    if info.handle_optim and (not optim_state_dict or not optim_state_dict["state"]):
        raise RuntimeError(
            "The option indicates that model state_dict is required to save, "
            f"or load but optim state_dict is empty. {optim_state_dict}"
        )

    for key, param in model_state_dict.items():
        if FLAT_PARAM in key:
            raise RuntimeError(
                f"{key} contains {FLAT_PARAM}. This can happen if the model "
                "is not the root module."
            )


def _state_dict_fn(obj: Union[nn.Module, torch.optim.Optimizer], api: str) -> Callable:
    call = getattr(obj, api)
    if call in _patched_state_dict:
        call = functools.partial(getattr(obj.__class__, api), self=obj)
    return call


def _get_model_state_dict(model: nn.Module, info: _StateDictInfo) -> Dict[str, Any]:
    with info.fsdp_context():
        state_dict = _state_dict_fn(model, "state_dict")()

    for key in list(state_dict.keys()):
        fqns = _get_fqns(model, key)
        assert len(fqns) == 1
        fqn = next(iter(fqns))
        if fqn != key:
            # As we only support FSDP, DDP, and TP, the only case is
            # wrapper-based DDP. Verify the assumption is correct.
            def verify(key, fqn) -> bool:
                if len(fqn) >= len(key):
                    return False
                fqn_split = fqn.split(".")
                key_split = key.split(".")
                fqn_idx = 0
                for key_idx, key_name in enumerate(key_split):
                    if key_name == fqn_split[fqn_idx]:
                        fqn_idx += 1
                        if fqn_idx == len(fqn_split):
                            return key_idx == len(key_split) - 1
                    elif key_name == "module":
                        continue
                    else:
                        return False
                return True

            if not verify(key, fqn):
                raise RuntimeError(f"An unexpected key, {key}, exists. FQN is {fqn}")
            state_dict[fqn] = state_dict.pop(key)

    if not info.save_frozen_params:
        for key, param in model.named_parameters():
            if param.requires_grad:
                continue
            fqns = _get_fqns(model, key)
            for fqn in fqns:
                state_dict.pop(fqn)
    return state_dict


def _load_model_state_dict(
    model: nn.Module,
    state_dict: Dict[str, Any],
    info: _StateDictInfo,
) -> Dict[str, Any]:
    for key, _ in model.named_parameters():
        fqns = _get_fqns(model, key)
        fqns_with_ddp_prefix = _get_fqns(model, key, skip_ddp_prefix=False)
        for fqn, fqn_with_ddp_prefix in zip(fqns, fqns_with_ddp_prefix):
            if fqn != fqn_with_ddp_prefix:
                state_dict[fqn_with_ddp_prefix] = state_dict.pop(fqn)

    with info.fsdp_context():
        return _state_dict_fn(model, "load_state_dict")(state_dict)


def _init_optim_state(optim: torch.optim.Optimizer) -> None:
    """
    Initialize optim states by using a step with zero grads.
    """
    if optim.state:
        # The optimizer state is initialized.
        return

    for param_group in optim.param_groups:
        for param in param_group["params"]:
            if param.grad is not None:
                raise RuntimeError(
                    "distributed_state_dict can only be used if the optimizer "
                    "states are initialized (usually after one step() with "
                    "gradients) or gradients are None. For the later case, "
                    "distributed_state_dict will fake the gradients as zero "
                    "to initialize the optimizer states. However, the "
                    "gradients are not None."
                )
            if param.requires_grad:
                param.grad = torch.zeros_like(param)
    optim.step(closure=None)
    optim.zero_grad(set_to_none=True)


def _get_optim_state_dict(
    model: nn.Module,
    optims: Tuple[torch.optim.Optimizer],
    info: _StateDictInfo,
) -> Dict[str, Any]:
    optim_state_dict = {"state": {}, PG: []}
    for optim in optims:
        _init_optim_state(optim)
        osd = _state_dict_fn(optim, "state_dict")()
        if info.fsdp_modules:
            with info.fsdp_context():
                osd = FSDP.optim_state_dict(model, optim, osd)
        else:
            params = list(chain.from_iterable(g["params"] for g in optim.param_groups))
            param_pid_mapping = dict(zip(params, range(len(params))))
            fqn_pid_mapping = {}
            for key, param in model.named_parameters():
                fqns = _get_fqns(model, key)
                assert len(fqns) == 1
                fqn = next(iter(fqns))
                if param not in param_pid_mapping:
                    continue
                pid = param_pid_mapping[param]
                fqn_pid_mapping[fqn] = pid
                fqn_pid_mapping[pid] = fqn

            for key in list(osd["state"].keys()):
                fqn = fqn_pid_mapping[key]
                osd["state"][fqn] = osd["state"].pop(key)

            for group in osd[PG]:
                group["params"] = [fqn_pid_mapping[pid] for pid in group["params"]]

        optim_state_dict["state"].update(osd["state"])
        optim_state_dict[PG].extend(osd[PG])

    return optim_state_dict


def _split_optim_state_dict(
    model: nn.Module,
    optim: torch.optim.Optimizer,
    optim_state_dict: Dict[str, Any],
    info: _StateDictInfo,
) -> Dict[str, Any]:
    """
    Extract the corresponding optim state_dict from ``optim_state_dict`` for
    ``optim`` and return the result optim state_dict.

    Args:
        model (nn.Module): the root model.
        optim (torch.optim.Optimizer): the optimizer.
        optim_state_dict (Dict[str, Any]): the superset optim state_dict that
            contains the optim state_dict of ``optim``.
        info (_StateDictInfo): state dict information.

    Returns:
        The optim state_dict of ``optim``.
    """

    return_osd = {"state": {}, PG: []}
    param_group_ids = set()

    for param_group in optim.param_groups:
        for param in param_group["params"]:
            if not param.requires_grad:
                continue
            for fqn in info.fqn_param_mapping[param]:
                return_osd["state"][fqn] = optim_state_dict["state"][fqn]
                for loaded_param_group in optim_state_dict[PG]:
                    if fqn in loaded_param_group["params"]:
                        param_group_ids.add(id(loaded_param_group))

    for param_group in optim_state_dict[PG]:
        if id(param_group) in param_group_ids:
            return_osd[PG].append(param_group)
    return return_osd


def _load_optim_state_dict(
    model: nn.Module,
    optims: Tuple[torch.optim.Optimizer],
    state_dict: Dict[str, Any],
    info: _StateDictInfo,
) -> None:
    for optim in optims:
        optim_state_dict = _split_optim_state_dict(model, optim, state_dict, info)
        if info.fsdp_modules:
            with info.fsdp_context():
                optim_state_dict = FSDP.optim_state_dict_to_load(
                    model, optim, optim_state_dict
                )

        # Note that we do not have to convert the FQN back to param id here if
        # the optim is initizlied by the `_init_optim_state()`. The way
        # torch.optim.Optimizer.load_state_dict() is able to directly map
        # the FQN to param id by using the order saved in the param group.
        _init_optim_state(optim)
        _state_dict_fn(optim, "load_state_dict")(optim_state_dict)


def distributed_state_dict(
    model: nn.Module,
    optims: Iterable[torch.optim.Optimizer] = tuple(),
    *,
    model_only: bool = False,
    optim_only: bool = False,
    options: Optional[DistributedStateDictOptions] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return the model state_dict and optimizers state_dict.

    The main difference of ``distributed_state_dict`` and tradtional
    ``module.state_dict`` is that ``distributed_state_dict`` convert all the
    FQNs (fully-qualified names) to the canonical FQNs. Here, canonical means
    the FQN based on a parameter's position in nn.Module hierarchy. A canonical
    FQN to a parameter is the FQN returned by ``module.named_parameters()``
    or ``module.named_buffers()`` when ``module`` is not distributed by
    any parallelisms. Another difference is that ``distributed_state_dict``
    converts the parameter IDs in the optimizer state_dict to the canoical FQNs.


    ``distributed_state_dict`` can process any module that is parallelized by
    ``FSDP/fully_shard``, ``DDP/replicate``, ``tensor_parallel`` and any
    combination of the previous parallelisms. ``distributed_state_dict`` can
    also process a model that is not parallelized at all. In such a case,
    ``distributed_state_dict`` only performs one function -- converting the
    optimizer parameter IDs to the canoical FQNs.

    Example:
        model = fully_shard(model)
        _apply_optimizer_in_backward(
            torch.optim.Adam, model.parameters(), {"lr": 1e-3}
        )
        optims = _get_in_backward_optimizers(model)
        model_state_dict, optim_state_dict = distributed_state_dict(model, optims)
        distributed_load_state_dict(
            model, optims, model_state_dict, optim_state_dict
        )

    Args:
        model (nn.Module): the nn.Module to the model.
        optims (Iterable[Optimizer]): The optimizers that are used to optimize
            ``model``. Note that optims accept multiple optimizers so the typing
            is Iterable. If optims is empty, the returned optimizer state_dict
            is empty.
        model_only (bool): if model_only is True, the returned optimizer
            state_dict will be empty (default: False)

        optim_only (bool): if optim_only is True, the returned model state_dict
            will be empty (default: False)
        options (DistributedStateDictOptions): the options to control how
            model state_dict and optimizer state_dict should be returned. See
            `DistributedStateDictOptions` for the details.
    Returns:
        A tuple of state_dict. The first one is model state_dict and the second
        one is optimizer state_dict. The model state_dict will be empty if
        `optim_only` is True. The optimizer state_dict will be empty if
        `model_only` is True or `optims` is empty.
    """
    optims = tuple(optims)
    info = _verify_options(model, optims, model_only, optim_only, options)
    model_state_dict = _get_model_state_dict(model, info)
    optim_state_dict = _get_optim_state_dict(model, optims, info)
    _verify_state_dict(model_state_dict, optim_state_dict, info)
    return model_state_dict, optim_state_dict


def distributed_load_state_dict(
    model: nn.Module,
    optims: Iterable[torch.optim.Optimizer] = tuple(),
    *,
    model_state_dict: Dict[str, Any] = {},
    optim_state_dict: Dict[str, Any] = {},
    model_only: bool = False,
    optim_only: bool = False,
    options: Optional[DistributedStateDictOptions] = None,
) -> None:
    """Load the model state_dict and optimizers state_dict.

    The counterpart of ``distributed_state_dict`` to load the state_dict
    generated by ``distributed_state_dict`` back to the model and optimizers.
    The given ``model_state_dict`` and ``optim_state_dict`` do not have to be
    returned by ``distributed_state_dict`` but must meet the following
    conditions:
        1. All FQNs are canoical FQNs as defined in ``distributed_state_dict``.
        2. If a tensor is sharded, it must be a ShardedTensor or DTensor.
        3. Optimizer state_dict must contain the canoical FQNs instead of
           parameter IDs.

    Args:
        model (nn.Module): the nn.Module to the model.
        optims (Iterable[Optimizer]): The optimizers that are used to optimize
            ``model``. Note that optims accept multiple optimizers so the typing
            is Iterable. ``optims`` can be an empty Iterable.
        model_only (bool): if model_only is True, only the model state_dict will
            be loaded (default: False)
        optim_only (bool): if optim_only is True, only the optimizer state_dict
            will be loaded (default: False)
        options (DistributedStateDictOptions): the options to control how
            model state_dict and optimizer state_dict should be loaded. See
            `DistributedStateDictOptions` for the details.
    Returns:
        None
    """
    optims = tuple(optims)
    info = _verify_options(model, optims, model_only, optim_only, options)
    _verify_state_dict(model_state_dict, optim_state_dict, info)
    _load_model_state_dict(model, model_state_dict, info)
    _load_optim_state_dict(model, optims, optim_state_dict, info)


def patch_model_state_dict(
    model: nn.Module,
    *,
    options: Optional[DistributedStateDictOptions] = None,
) -> None:
    """Patch the ``state_dict`` and ``load_state_dict`` attributes of ``model``.

    Patch the ``state_dict`` and ``load_state_dict`` attributes of ``model`` to
    be a partial function to call ``distributed_state_dict``.

    Example:
        model = fully_shard(model)
        patch_model_state_dict(model)

        state_dict = model.state_dict()
        model.load_state_dict(state_dict)

    Args:
        model (nn.Module): the nn.Module to the model.
        options (DistributedStateDictOptions): the options to control how
            model state_dict and optimizer state_dict should be loaded. See
            `DistributedStateDictOptions` for the details.
    Returns:
        None
    """

    _state_dict_call = functools.partial(
        distributed_state_dict,
        model=model,
        optims=tuple(),
        model_only=True,
        options=options,
    )
    state_dict_call = lambda: _state_dict_call()[0]
    model.state_dict = state_dict_call

    _load_state_dict_call = functools.partial(
        distributed_load_state_dict,
        model=model,
        optims=tuple(),
        model_only=True,
        options=options,
    )
    load_state_dict_call = lambda state_dict: _load_state_dict_call(
        state_dict=state_dict
    )[1]
    model.load_state_dict = load_state_dict_call

    _patched_state_dict.add(state_dict_call)
    _patched_state_dict.add(load_state_dict_call)


def patch_optimizer_state_dict(
    model: nn.Module,
    optims: Tuple[torch.optim.Optimizer],
    *,
    options: Optional[DistributedStateDictOptions] = None,
) -> None:
    """Patch the ``state_dict`` and ``load_state_dict`` attributes of ``optims``.

    Patch the ``state_dict`` and ``load_state_dict`` attributes of ``optims`` to
    be a partial function to call ``distributed_state_dict``.

    Note that if there are multiple optimizers, all of the optims will be patched.
    So users only need to call one of the state_dict() to get the full result.

    Example:
        model = fully_shard(model)
        _apply_optimizer_in_backward(
            torch.optim.Adam, model.parameters(), {"lr": 1e-3}
        )
        optims = _get_in_backward_optimizers(model)
        patch_optimizer_state_dict(model, optims)

        state_dict = optims[0].state_dict()
        optims[0].load_state_dict(state_dict)

    Args:
        model (nn.Module): the nn.Module to the model.
        options (DistributedStateDictOptions): the options to control how
            model state_dict and optimizer state_dict should be loaded. See
            `DistributedStateDictOptions` for the details.
    Returns:
        None
    """

    _state_dict_call = functools.partial(
        distributed_state_dict,
        model=model,
        optims=optims,
        optim_only=True,
        options=options,
    )
    state_dict_call = lambda: _state_dict_call()[1]
    _load_state_dict_call = functools.partial(
        distributed_load_state_dict,
        model=model,
        optims=optims,
        optim_only=True,
        options=options,
    )
    load_state_dict_call = lambda state_dict: _load_state_dict_call(
        optim_state_dict=state_dict
    )
    _patched_state_dict.add(state_dict_call)
    _patched_state_dict.add(load_state_dict_call)

    for optim in optims:
        optim.state_dict = state_dict_call
        optim.load_state_dict = load_state_dict_call
