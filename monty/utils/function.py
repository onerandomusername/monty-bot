"""Utilities for interaction with functions."""

import functools
import inspect
import types
import typing as t

from monty.log import get_logger


log = get_logger(__name__)

AnyCallable = t.Callable[..., t.Any]
FuncT = t.TypeVar("FuncT", bound=AnyCallable)
OtherFuncT = t.TypeVar("OtherFuncT", bound=AnyCallable)

Argument = t.Union[int, str]
BoundArgs = t.OrderedDict[str, t.Any]
Decorator = t.Callable[[FuncT], OtherFuncT]
ArgValGetter = t.Callable[[BoundArgs], t.Any]


class GlobalNameConflictError(Exception):
    """Raised when there's a conflict between the globals used to resolve annotations of wrapped and its wrapper."""


def get_arg_value(name_or_pos: Argument, arguments: BoundArgs) -> t.Any:
    """
    Return a value from `arguments` based on a name or position.

    `arguments` is an ordered mapping of parameter names to argument values.

    Raise TypeError if `name_or_pos` isn't a str or int.
    Raise ValueError if `name_or_pos` does not match any argument.
    """
    if isinstance(name_or_pos, int):
        # Convert arguments to a tuple to make them indexable.
        arg_values = tuple(arguments.items())
        arg_pos = name_or_pos

        try:
            _, value = arg_values[arg_pos]
            return value
        except IndexError:
            raise ValueError(f"Argument position {arg_pos} is out of bounds.") from None
    elif isinstance(name_or_pos, str):
        arg_name = name_or_pos
        try:
            return arguments[arg_name]
        except KeyError:
            raise ValueError(f"Argument {arg_name!r} doesn't exist.") from None
    else:
        raise TypeError("'arg' must either be an int (positional index) or a str (keyword).")


def get_arg_value_wrapper(
    decorator_func: t.Callable[[ArgValGetter], Decorator[FuncT, OtherFuncT]],
    name_or_pos: Argument,
    func: t.Callable[[t.Any], t.Any] = None,
) -> Decorator[FuncT, OtherFuncT]:
    """
    Call `decorator_func` with the value of the arg at the given name/position.

    `decorator_func` must accept a callable as a parameter to which it will pass a mapping of
    parameter names to argument values of the function it's decorating.

    `func` is an optional callable which will return a new value given the argument's value.

    Return the decorator returned by `decorator_func`.
    """

    def wrapper(args: BoundArgs) -> t.Any:
        value = get_arg_value(name_or_pos, args)
        if func:
            value = func(value)
        return value

    return decorator_func(wrapper)


def get_bound_args(func: t.Callable, args: t.Tuple, kwargs: t.Dict[str, t.Any]) -> BoundArgs:
    """
    Bind `args` and `kwargs` to `func` and return a mapping of parameter names to argument values.

    Default parameter values are also set.
    """
    sig = inspect.signature(func)
    bound_args = sig.bind(*args, **kwargs)
    bound_args.apply_defaults()

    return bound_args.arguments


def update_wrapper_globals(
    wrapper: FuncT,
    wrapped: AnyCallable,
    *,
    ignored_conflict_names: t.Union[set[str], frozenset[str]] = None,
) -> FuncT:
    """
    Update globals of `wrapper` with the globals from `wrapped`.

    For forwardrefs in command annotations discordpy uses the __global__ attribute of the function
    to resolve their values, with decorators that replace the function this breaks because they have
    their own globals.

    This function creates a new function functionally identical to `wrapper`, which has the globals replaced with
    a merge of `wrapped`s globals and the `wrapper`s globals.

    An exception will be raised in case `wrapper` and `wrapped` share a global name that is used by
    `wrapped`'s typehints and is not in `ignored_conflict_names`,
    as this can cause incorrect objects being used by discordpy's converters.
    """
    if ignored_conflict_names is None:
        ignored_conflict_names = frozenset()

    annotation_global_names = (
        ann.split(".", maxsplit=1)[0] for ann in wrapped.__annotations__.values() if isinstance(ann, str)
    )
    # Conflicting globals from both functions' modules that are also used in the wrapper and in wrapped's annotations.
    shared_globals = set(wrapper.__code__.co_names) & set(annotation_global_names)
    shared_globals &= set(wrapped.__globals__) & set(wrapper.__globals__) - ignored_conflict_names
    if shared_globals:
        raise GlobalNameConflictError(
            "wrapper and the wrapped function share the following "
            f"global names used by annotations: {', '.join(shared_globals)}. Resolve the conflicts or add "
            "the name to the `ignored_conflict_names` set to suppress this error if this is intentional."
        )

    new_globals = wrapper.__globals__.copy()
    new_globals.update((k, v) for k, v in wrapped.__globals__.items() if k not in wrapper.__code__.co_names)
    return types.FunctionType(
        code=wrapper.__code__,
        globals=new_globals,
        name=wrapper.__name__,
        argdefs=wrapper.__defaults__,
        closure=wrapper.__closure__,
    )  # type: ignore


def command_wraps(
    wrapped: AnyCallable,
    assigned: t.Sequence[str] = functools.WRAPPER_ASSIGNMENTS,
    updated: t.Sequence[str] = functools.WRAPPER_UPDATES,
    *,
    ignored_conflict_names: t.Union[set[str], frozenset[str]] = None,
) -> t.Callable[[FuncT], FuncT]:
    """Update the decorated function to look like `wrapped` and update globals for discordpy forwardref evaluation."""
    if ignored_conflict_names is None:
        ignored_conflict_names = frozenset()

    def decorator(wrapper: FuncT) -> FuncT:
        return functools.update_wrapper(
            update_wrapper_globals(wrapper, wrapped, ignored_conflict_names=ignored_conflict_names),
            wrapped,
            assigned,
            updated,
        )

    return decorator
