#  Copyright (c) 2020 zfit

# TODO(Mayou36): update docs above

import functools
import inspect
from abc import abstractmethod
from collections import OrderedDict, defaultdict
from contextlib import suppress
from typing import Callable, List, Optional, Tuple, Union, Iterable, Mapping

import numpy as np
import tensorflow as tf
from tensorflow.python.util.deprecation import deprecated

import zfit
from .baseobject import BaseObject
from .dimension import common_obs, common_axes, limits_overlap
from .interfaces import ZfitLimit, ZfitOrderableDimensional, ZfitSpace
from .. import z
from ..util import ztyping
from ..util.container import convert_to_container
from ..util.exception import (AxesNotSpecifiedError, IntentionNotUnambiguousError, LimitsUnderdefinedError,
                              MultipleLimitsNotImplementedError, NormRangeNotImplementedError, ObsNotSpecifiedError,
                              OverdefinedError, LimitsNotSpecifiedError, WorkInProgressError,
                              BreakingAPIChangeError, LimitsIncompatibleError, SpaceIncompatibleError,
                              ObsIncompatibleError, AxesIncompatibleError, ShapeIncompatibleError,
                              IllegalInGraphModeError, CoordinatesUnderdefinedError, CoordinatesIncompatibleError,
                              InvalidLimitSubspaceError, CannotConvertToNumpyError)


# Singleton
class Any:
    _singleton_instance = None

    def __new__(cls, *args, **kwargs):
        instance = cls._singleton_instance
        if instance is None:
            instance = super().__new__(cls)
            cls._singleton_instance = instance

        return instance

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._singleton_instance = None  # each subclass is a singleton of "itself"

    def __repr__(self):
        return '<Any>'

    def __lt__(self, other):
        return True

    def __le__(self, other):
        return True

    # def __eq__(self, other):
    #     return True

    def __ge__(self, other):
        return True

    def __gt__(self, other):
        return True

    # def __hash__(self):
    #     return


class AnyLower(Any):
    def __repr__(self):
        return '<Any Lower Limit>'

    # def __eq__(self, other):
    #     return False

    def __ge__(self, other):
        return False

    def __gt__(self, other):
        return False


class AnyUpper(Any):
    def __repr__(self):
        return '<Any Upper Limit>'

    # def __eq__(self, other):
    #     return False

    def __le__(self, other):
        return False

    def __lt__(self, other):
        return False


ANY = Any()
ANY_LOWER = AnyLower()
ANY_UPPER = AnyUpper()


@z.function_tf_input
def calculate_rect_area(rect_limits):
    lower, upper = rect_limits
    diff = upper - lower
    area = z.unstable.reduce_prod(diff, axis=-1)
    return area


@z.function_tf_input
def inside_rect_limits(x, rect_limits):
    if not x.shape.ndims > 1:
        raise ValueError("x has ndims <= 1, which is most probably not wanted. The default shape for array-like"
                         " structures is (nevents, n_obs).")
    lower, upper = rect_limits
    lower = z.convert_to_tensor(lower)
    upper = z.convert_to_tensor(upper)
    below_upper = tf.reduce_all(input_tensor=tf.less_equal(x, upper), axis=-1)  # if all obs inside
    above_lower = tf.reduce_all(input_tensor=tf.greater_equal(x, lower), axis=-1)
    inside = tf.logical_and(above_lower, below_upper)
    return inside


@z.function_tf_input
def filter_rect_limits(x, rect_limits):
    return tf.boolean_mask(tensor=x, mask=inside_rect_limits(x, rect_limits=rect_limits))


def convert_to_tensor_or_numpy(obj):
    if isinstance(obj, (tf.Tensor, tf.Variable)):
        return obj
    else:
        return np.array(obj)


def _sanitize_x_input(x, n_obs):
    x = z.convert_to_tensor(x)
    if not x.shape.ndims > 1 and n_obs > 1:
        raise ValueError("x has ndims <= 1, which is most probably not wanted. The default shape for array-like"
                         " structures is (nevents, n_obs).")
    elif x.shape.ndims <= 1 and n_obs == 1:
        if x.shape.ndims == 0:
            x = tf.broadcast_to(x, (1, 1))
        else:
            x = tf.expand_dims(x, axis=-1)
    if tf.get_static_value(x.shape[-1]) != n_obs:
        raise ShapeIncompatibleError("n_obs and the last dim of x do not agree. Assuming x has shape (..., n_obs)")
    return x


def convert_to_numpy(obj):
    if isinstance(obj, (tf.Tensor, tf.Variable)):
        return obj.numpy()
    else:
        return np.array(obj)


class Coordinates(ZfitOrderableDimensional):
    def __init__(self, obs=None, axes=None):
        obs, axes, n_obs = self._check_convert_obs_axes(obs, axes)
        self._obs = obs
        self._axes = axes
        self._n_obs = n_obs

    @staticmethod
    def _check_convert_obs_axes(obs, axes):
        if isinstance(obs, ZfitOrderableDimensional):
            if axes is not None:
                raise OverdefinedError("Cannot use (currently, please open an issue if desired) a"
                                       " ZfitOrderableDimensional as obs with axes not None")
            coord = obs
            return coord.obs, coord.axes, coord.n_obs
        obs = convert_to_obs_str(obs, container=tuple)
        axes = convert_to_axes(axes, container=tuple)
        if obs is None:
            if axes is None:
                raise CoordinatesUnderdefinedError("Neither obs nor axes specified")
            n_obs = len(axes)
        else:
            n_obs = len(obs)
            if axes is not None:
                if not len(obs) == len(axes):
                    raise CoordinatesIncompatibleError("obs and axes do not have the same length.")
        return obs, axes, n_obs

    @property
    def obs(self):
        return self._obs

    @property
    def axes(self):
        return self._axes

    @property
    def n_obs(self):
        return self._n_obs

    def with_obs(self, obs: Optional[ztyping.ObsTypeInput], allow_superset: bool = False, allow_subset: bool = False):
        if obs is None:  # drop obs, check if there are axes
            if self.axes is None:
                raise AxesIncompatibleError("cannot remove obs (using None) for a Space without axes")
            new_coords = type(self)(obs=obs, axes=self.axes)
        else:
            obs = _convert_obs_to_str(obs)

            if not frozenset(obs) == frozenset(self.obs):

                if not allow_superset and frozenset(obs).issuperset(self.obs):
                    raise ObsIncompatibleError(
                        f"Obs {obs} are a superset of {self.obs}, not allowed according to flag.")

                if not allow_subset and frozenset(obs).issubset(self.obs):
                    raise ObsIncompatibleError(
                        f"Obs {obs} are a subset of {self.obs}, not allowed according to flag.")
            new_indices = self.get_reorder_indices(obs=obs)
            new_obs = self._reorder_obs(indices=new_indices)
            new_axes = self._reorder_axes(indices=new_indices)
            new_coords = type(self)(obs=new_obs, axes=new_axes)
        return new_coords

    def with_axes(self, axes: Optional[ztyping.AxesTypeInput], allow_superset: bool = False,
                  allow_subset: bool = False) -> "zfit.Space":
        """Sort by `axes` and return the new instance. `None` drops the axes.

        Args:
            axes ():
            allow_superset (bool): Allow `axes` to be a superset of the `Spaces` axes

        Returns:
            :py:class:`~zfit.Space`
        """
        if axes is None:  # drop axes
            if self.obs is None:
                raise ObsIncompatibleError("Cannot remove axes (using None) for a Space without obs")
            new_coords = type(self)(obs=self.obs, axes=axes)
        else:
            axes = _convert_axes_to_int(axes)
            if not frozenset(axes) == frozenset(self.axes):
                if not allow_superset and frozenset(axes).issuperset(self.axes):
                    raise AxesIncompatibleError(
                        f"Axes {axes} are a superset of {self.axes}, not allowed according to flag.")

                if not allow_subset and frozenset(axes).issubset(self.axes):
                    raise AxesIncompatibleError(
                        f"Axes {axes} are a subset of {self.axes}, not allowed according to flag.")
            new_indices = self.get_reorder_indices(axes=axes)
            new_obs = self._reorder_obs(indices=new_indices)
            new_axes = self._reorder_axes(indices=new_indices)
            new_coords = type(self)(obs=new_obs, axes=new_axes)
        return new_coords

    def with_autofill_axes(self, overwrite: bool = False) -> "zfit.Space":
        """Return a :py:class:`~zfit.Space` with filled axes corresponding to range(len(n_obs)).

        Args:
            overwrite (bool): If `self.axes` is not None, replace the axes with the autofilled ones.
                If axes is already set, don't do anything if `overwrite` is False.

        Returns:
            :py:class:`~zfit.Space`
        """
        if self.axes and not overwrite:
            raise ValueError("overwrite is not allowed but axes are already set.")
        new_coords = type(self)(obs=self.obs, axes=range(self.n_obs))
        return new_coords

    def _reorder_obs(self, indices: Tuple[int]) -> ztyping.ObsTypeReturn:
        obs = self.obs
        if obs is not None:
            obs = tuple(obs[i] for i in indices)
        return obs

    def _reorder_axes(self, indices: Tuple[int]) -> ztyping.AxesTypeReturn:
        axes = self.axes
        if axes is not None:
            axes = tuple(axes[i] for i in indices)
        return axes

    def get_reorder_indices(self, obs: ztyping.ObsTypeInput = None,
                            axes: ztyping.AxesTypeInput = None) -> Tuple[int]:
        """Indices that would order `self.obs` as `obs` respectively `self.axes` as `axes`.

        Args:
            obs ():
            axes ():

        Returns:

        """
        obs_none = obs is None
        axes_none = axes is None

        obs_is_defined = self.obs is not None and not obs_none
        axes_is_defined = self.axes is not None and not axes_none
        if not (obs_is_defined or axes_is_defined):
            raise ValueError(
                "Neither the `obs` (argument and on instance) nor `axes` (argument and on instance) are defined.")

        if obs_is_defined:
            old, new = self.obs, [o for o in obs if o in self.obs]
        else:
            old, new = self.axes, [a for a in axes if a in self.axes]

        new_indices = _reorder_indices(old=old, new=new)
        return new_indices

    def reorder_x(self, x: Union[tf.Tensor, np.ndarray], *, x_obs: ztyping.ObsTypeInput = None,
                  x_axes: ztyping.AxesTypeInput = None, func_obs: ztyping.ObsTypeInput = None,
                  func_axes: ztyping.AxesTypeInput = None) -> Union[tf.Tensor, np.ndarray]:
        """Reorder x in the last dimension either according to its own obs or assuming a function ordered with func_obs.

        There are two obs or axes around: the one associated with this Coordinate object and the one associated with x.
        If x_obs or x_axes is given, then this is assumed to be the obs resp. the axes of x and x will be reordered
        according to `self.obs` resp. `self.axes`.

        If func_obs resp. func_axes is given, then x is assumed to have `self.obs` resp. `self.axes` and will be
        reordered to align with a function ordered with `func_obs` resp. `func_axes`.

        Switching `func_obs` for `x_obs` resp. `func_axes` for `x_axes` inverts the reordering of x.

        Args:
            x (tensor-like): Tensor to be reordered, last dimension should be n_obs resp. n_axes
            x_obs: Observables associated with x. If both, x_obs and x_axes are given, this has precedency over the
                latter.
            x_axes: Axes associated with x.
            func_obs: Observables associated with a function that x will be given to. Reorders x accordingly and assumes
                self.obs to be the obs of x. If both, `func_obs` and `func_axes` are given, this has precedency over the
                latter.
            func_axes: Axe associated with a function that x will be given to. Reorders x accordingly and assumes
                self.axes to be the axes of x.

        Returns:

        """
        x_reorder = x_obs is not None or x_axes is not None
        func_reorder = func_obs is not None or func_axes is not None
        if not (x_reorder ^ func_reorder):
            raise ValueError("Either specify `x_obs/axes` or `func_obs/axes`, not both.")
        obs_defined = x_obs is not None or func_obs is not None
        axes_defined = x_axes is not None or func_axes is not None
        if obs_defined and self.obs:
            if x_reorder:
                coord_old = x_obs
                coord_new = self.obs
            elif func_reorder:
                coord_new = func_obs
                coord_old = self.obs
            else:
                assert False, 'bug, should never be reached'

        elif axes_defined and self.axes:
            if x_reorder:
                coord_old = x_axes
                coord_new = self.axes
            elif func_reorder:
                coord_new = func_axes
                coord_old = self.axes
            else:
                assert False, 'bug, should never be reached'
        else:
            raise ValueError("Obs and self.obs or axes and self. axes not properly defined. Can only reorder on defined"
                             " coordinates.")

        new_indices = _reorder_indices(old=coord_old, new=coord_new)

        x = z.unstable.gather(x, indices=new_indices, axis=-1)
        return x

    def __eq__(self, other):
        if not isinstance(other, Coordinates):
            return NotImplemented
        obs_equal = False
        axes_equal = False
        if self.obs is not None and other.obs is not None:
            obs_equal = frozenset(self.obs) == frozenset(other.obs)

        if self.axes is not None and other.axes is not None:
            axes_equal = frozenset(self.axes) == frozenset(other.axes)
        equal = obs_equal or axes_equal
        return equal

    def __hash__(self):
        return 42  # always check with equal...  maybe change in future, use dict that checks for different things.

    def __repr__(self):
        return f"<zfit Coordinates obs={self.obs}, axes={self.axes}"


class Limit(ZfitLimit):
    def __init__(self, limit_fn=None, rect_limits=None, n_obs=None):
        super().__init__()
        limit_fn, rect_limits, n_obs, is_rect, sublimits = self._check_convert_input_limits(limits_fn=limit_fn,
                                                                                            rect_limits=rect_limits,
                                                                                            n_obs=n_obs)
        self._limit_fn = limit_fn
        self._rect_limits = rect_limits
        self._n_obs = n_obs
        self._is_rect = is_rect
        self._sublimits = sublimits

    def _check_convert_input_limits(self, limits_fn, rect_limits, n_obs):
        if isinstance(limits_fn, ZfitLimit):
            if rect_limits is not None or n_obs:
                raise OverdefinedError("limits_fn is a ZfitLimit. rect_limits and n_obs must not be specified.")
            limit = limits_fn
            return limit.limit_fn, limit.rect_limits, limit.n_obs, limit.has_rect_limits, (self,)
        limits_are_rect = True
        if limits_fn is False:
            if rect_limits in (False, None):
                return False, False, n_obs, None, (self,)
        elif limits_fn is None:
            if rect_limits is False:
                return False, False, n_obs, None, (self,)
            elif rect_limits is None:
                return None, None, n_obs, None, (self,)
            else:  # start from limits are anything, rect is None
                limits_fn = rect_limits
                rect_limits = None

        if not callable(limits_fn):  # limits_fn is actually rect_limits
            rect_limits = limits_fn
            limits_fn = None

        else:
            limits_are_rect = False
            if rect_limits in (None, False):
                raise ValueError("Limits given as a function need also rect_limits, cannot be None or False")
        try:
            lower, upper = rect_limits
        except TypeError as err:
            raise TypeError(
                "The outermost shape of `rect_limits` has to be 2 to represent (lower, upper).") from err

        # if not isinstance(lower, (np.ndarray, tf.Tensor)):

        lower = self._sanitize_rect_limit(lower)
        upper = self._sanitize_rect_limit(upper)

        lower_nobs = lower.shape[-1]
        upper_nobs = upper.shape[-1]
        # lower.shape.assert_is_compatible_with(upper.shape)
        tf.assert_equal(lower_nobs, upper_nobs, message="Last dimension of lower and upper have to coincide.")
        if n_obs is not None:
            tf.assert_equal(lower_nobs, n_obs,
                            message="Inferred last dimension (n_obs) does not coincide with given n_obs")
        n_obs = tf.get_static_value(lower_nobs)
        rect_limits = (lower, upper)

        # create sublimits to iterate if possible
        sublimits = []
        if limits_are_rect and n_obs > 1:
            for i in range(n_obs):
                low = tf.gather(lower, (i,), axis=-1)
                up = tf.gather(upper, (i,), axis=-1)
                sublimits.append(type(self)(rect_limits=(low, up), n_obs=1))
        else:
            sublimits.append(self)

        sublimits = tuple(sublimits)

        return limits_fn, rect_limits, n_obs, limits_are_rect, sublimits

    @staticmethod
    def _sanitize_rect_limit(limit):
        limit = convert_to_tensor_or_numpy(limit)
        if len(limit.shape) == 0:
            limit = z.unstable.broadcast_to(limit, shape=(1, 1))
        if len(limit.shape) == 1:
            limit = z.unstable.expand_dims(limit, axis=0)
        return limit

    @property
    def has_rect_limits(self) -> bool:
        return self.has_limits and self._is_rect

    @property
    def rect_limits(self):

        rect_limits = self._rect_limits
        if rect_limits in (None, False):
            return rect_limits
        lower = z.convert_to_tensor(rect_limits[0])
        upper = z.convert_to_tensor(rect_limits[1])
        return lower, upper

    @property
    def _rect_limits_np(self):
        """Return the rectangular limits as `np.ndarray`. Raises error if not possible.

        Returns:
            (lower, upper):

        Raises:
            CannotConvertToNumpyError: In case the conversion fails.
        """
        lower, upper = self._rect_limits

        lower = z.unstable._try_convert_numpy(lower)
        upper = z.unstable._try_convert_numpy(upper)
        return (lower, upper)

    @property
    def limit_fn(self):
        return self._limit_fn

    def rect_area(self) -> float:
        return calculate_rect_area(rect_limits=self.rect_limits)

    def inside(self, x, guarantee_limits=False):
        x = _sanitize_x_input(x, n_obs=self.n_obs)
        if guarantee_limits and self.has_rect_limits:
            return tf.broadcast_to(True, x.shape)
        else:
            return self._inside(x, guarantee_limits)

    def _inside(self, x, guarantee_limits):
        if self.has_rect_limits:
            return inside_rect_limits(x, rect_limits=self.rect_limits)
        else:
            return self._limit_fn(x)

    def filter(self, x, guarantee_limits):
        x = _sanitize_x_input(x, n_obs=self.n_obs)
        if guarantee_limits and self.has_rect_limits:
            return x
        if self.has_rect_limits:
            return filter_rect_limits(x, rect_limits=self.rect_limits)
        else:
            return self._filter(x, guarantee_limits)

    def _filter(self, x, guarantee_limits):
        return tf.boolean_mask(tensor=x, mask=self.inside(x, guarantee_limits=guarantee_limits))

    @property
    def limits_not_set(self):
        return self.rect_limits is None

    @property
    def has_limits(self):
        return self.rect_limits is not False and not self.limits_not_set

    @property
    def rect_lower(self):
        return self.rect_limits[0]

    @property
    def rect_upper(self):
        return self.rect_limits[1]

    @property
    def n_obs(self) -> int:
        return self._n_obs

    def __eq__(self, other):
        if not isinstance(other, ZfitLimit):
            return NotImplemented
        return self.equal(other, allow_graph=False)

    def __le__(self, other):
        if not isinstance(other, ZfitLimit):
            return NotImplemented
        return self.less_equal(other, allow_graph=False)

    def less_equal(self, other, allow_graph=True):
        return less_equal_limits(self, other, allow_graph=allow_graph)

    def __iter__(self):
        yield from self._sublimits

    def __hash__(self) -> int:
        objects = (self._limit_fn, self.n_obs)  # not rect limits, not hashable and unprecise
        return hash(tuple(objects))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Limit):
            return NotImplemented
        return self.equal(other=other, allow_graph=False)

    def equal(self, other, allow_graph=True):
        return equal_limits(self, other, allow_graph=allow_graph)


def less_equal_limits(limit1: Limit, limit2: Limit, allow_graph=True) -> bool:
    try:
        lower1, upper1 = limit1._rect_limits_np
        lower2, upper2 = limit2._rect_limits_np
    except CannotConvertToNumpyError:
        if not allow_graph:
            raise IllegalInGraphModeError(
                "Cannot use equality in graph mode, e.g. inside a `tf.function` decorated "
                "function. To retrieve a symbolic Tensor, use `.equal(..., allow_graph=True)`")
        else:
            lower1, upper1 = limit1.rect_limits
            lower2, upper2 = limit2.rect_limits

    lower_le = z.unstable.reduce_all(z.unstable.less_equal(lower1, lower2), axis=-1)
    upper_le = z.unstable.reduce_all(z.unstable.less_equal(upper1, upper2), axis=-1)
    rect_limits_le = z.unstable.logical_and(lower_le, upper_le)
    funcs_equal = limit1.limit_fn == limit2.limit_fn
    return z.unstable.logical_and(rect_limits_le, funcs_equal)


def equal_limits(limit1: Limit, limit2: Limit, allow_graph=True) -> bool:
    try:
        lower, upper = limit1._rect_limits_np
        lower_other, upper_other = limit2._rect_limits_np
    except CannotConvertToNumpyError:
        if not allow_graph:
            raise IllegalInGraphModeError(
                "Cannot use equality in graph mode, e.g. inside a `tf.function` decorated "
                "function. To retrieve a symbolic Tensor, use `.equal(..., allow_graph=True)`")
        else:
            lower, upper = limit1.rect_limits
            lower_other, upper_other = limit2.rect_limits

    # TODO add tolerances
    rect_limits_equal = z.unstable.reduce_all(z.unstable.allclose((lower, upper), (lower_other, upper_other)))

    funcs_equal = limit1.limit_fn == limit2.limit_fn
    return z.unstable.logical_and(rect_limits_equal, funcs_equal)


class BaseSpace(ZfitSpace, BaseObject):

    def __init__(self, obs, axes, name, **kwargs):
        super().__init__(name, **kwargs)
        coords = Coordinates(obs, axes)
        self.coords = coords

    def inside(self, x: tf.Tensor, guarantee_limits: bool = False) -> tf.Tensor:
        x = _sanitize_x_input(x, n_obs=self.n_obs)
        if self.has_rect_limits and guarantee_limits:
            return tf.broadcast_to(True, x.shape)
        inside = self._inside(x, guarantee_limits)
        return inside

    @abstractmethod
    def _inside(self, x, guarantee_limits):
        raise NotImplementedError

    def filter(self, x: tf.Tensor, guarantee_limits: bool = False) -> tf.Tensor:
        if self.has_rect_limits and guarantee_limits:
            return x
        filtered = self._filter(x, guarantee_limits)
        return filtered

    def _filter(self, x, guarantee_limits):
        filtered = tf.boolean_mask(tensor=x, mask=self.inside(x, guarantee_limits=guarantee_limits))
        return filtered

    @property
    def n_obs(self) -> int:  # TODO(naming): better name? Like rank?
        """Return the number of observables/axes.

        Returns:
            int >= 1
        """

        return self.coords.n_obs

    @property
    def obs(self) -> ztyping.ObsTypeReturn:
        """The observables ("axes with str")the space is defined in.

        Returns:

        """
        return self.coords.obs

    @property
    def axes(self) -> ztyping.AxesTypeReturn:
        """The axes ("obs with int") the space is defined in.

        Returns:

        """
        return self.coords.axes

    @property
    def n_limits(self) -> int:
        return len(tuple(self))

    def __iter__(self) -> Iterable[ZfitSpace]:
        yield self

    # TODO: remove, in coords

    # TODO
    def _check_convert_input_axes(self, axes: ztyping.AxesTypeInput,
                                  allow_none: bool = False) -> ztyping.AxesTypeReturn:
        if axes is None:
            if allow_none:
                return None
            else:
                raise AxesNotSpecifiedError("TODO: Cannot be None")
        if isinstance(axes, ZfitSpace):
            axes = axes.axes
        else:
            axes = convert_to_container(value=axes, container=tuple)  # TODO(Mayou36): extend like _check_obs?

        return axes

    # TODO: remove, in coords
    def _check_convert_input_obs(self, obs: ztyping.ObsTypeInput,
                                 allow_none: bool = False) -> ztyping.ObsTypeReturn:
        """Input check: Convert `NOT_SPECIFIED` to None or check if obs are all strings.

        Args:
            obs (str, List[str], None, NOT_SPECIFIED):

        Returns:
            type:
        """
        if obs is None:
            if allow_none:
                return None
            else:
                raise ObsNotSpecifiedError("TODO: Cannot be None")

        if isinstance(obs, ZfitSpace):
            obs = obs.obs
        else:
            obs = convert_to_container(obs, container=tuple)
            obs_not_str = tuple(o for o in obs if not isinstance(o, str))
            if obs_not_str:
                raise ValueError("The following observables are not strings: {}".format(obs_not_str))
        return obs

    def _check_coords_allowed(self, obs=None, axes=None, allow_superset=False, allow_subset=True):
        to_check = []
        if obs is not None and self.obs is not None:
            to_check.append(obs, self.obs)
        if axes is not None and self.axes is not None:
            to_check.append(axes, self.axes)

        for coord, self_coord in to_check:
            coord = frozenset(coord)
            self_coord = frozenset(self_coord)
            if coord != self_coord:

                if not allow_superset and coord.issuperset(self_coord):
                    raise CoordinatesIncompatibleError(f"Superset is not allowed, but {coord} is a superset"
                                                       f" of {self_coord}")

                if not allow_subset and coord.issubset(self_coord):
                    raise CoordinatesIncompatibleError(f"subset is not allowed, but {coord} is a subset"
                                                       f" of {self_coord}")

    def __repr__(self):
        class_name = str(self.__class__).split('.')[-1].split('\'')[0]
        return f"<zfit {class_name} obs={self.obs}, axes={self.axes}, limits={self.has_limits}>"

    def __add__(self, other):
        if not isinstance(other, ZfitSpace):
            raise TypeError("Cannot add a {} and a {}".format(type(self), type(other)))
        return add_spaces_new(self, other)

    def equal(self, other, allow_graph):
        if not isinstance(other, ZfitSpace):
            return NotImplemented
        return equal_space(other, allow_graph=allow_graph)
        limits_equal = self.rect_limits == other.rect_limits  # TODO: improve! What about 'inside'?
        coords_equal = self.coords == other.coords
        return z.unstable.logical_and(limits_equal, coords_equal)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ZfitSpace):
            return NotImplemented
        return self.equal(other=other, allow_graph=False)

    def __le__(self, other):
        if not isinstance(other, type(self)):
            return NotImplemented
        return less_equal_space(other)

    def add(self, *other: ztyping.SpaceOrSpacesTypeInput):
        """Add the limits of the spaces. Only works for the same obs.

        In case the observables are different, the order of the first space is taken.

        Args:
            other (:py:class:`~zfit.Space`):

        Returns:
            :py:class:`~zfit.Space`:
        """
        # other = convert_to_container(other, container=list)
        new_space = add_spaces_new(self, *other)
        return new_space

    def combine(self, *other: ztyping.SpaceOrSpacesTypeInput) -> ZfitSpace:
        """Combine spaces with different obs (but consistent limits).

        Args:
            other (:py:class:`~zfit.Space`):

        Returns:
            :py:class:`~zfit.Space`:
        """
        # other = convert_to_container(other, container=list)
        new_space = combine_spaces_new(self, *other)
        return new_space

    def __mul__(self, other):
        return self.combine(other)

    def __ge__(self, other):
        return NotImplemented

    def __eq__(self, other):
        if not isinstance(other, ZfitSpace):
            return NotImplemented
        return equal_space(self, other)

    def __hash__(self):
        limits_frozen = tuple(((key, tuple(ldict.items())) for key, ldict in self._limits_dict.items()))
        hash_val = hash(tuple((limits_frozen, hash(self.coords))))
        return hash_val

    def reorder_x(self, x, x_obs, x_axes, func_obs, func_axes):
        return self.coords.reorder_x(x, x_obs=x_obs, x_axes=x_axes,
                                     func_obs=func_obs, func_axes=func_axes)


class Space(BaseSpace):
    AUTO_FILL = object()
    ANY = ANY
    ANY_LOWER = ANY_LOWER  # TODO: needed? or move everything inside?
    ANY_UPPER = ANY_UPPER

    def __init__(self, obs: Optional[ztyping.ObsTypeInput] = None, limits: Optional[ztyping.LimitsTypeInput] = None,
                 axes=None, rect_limits=None,
                 name: Optional[str] = "Space"):
        """Define a space with the name (`obs`) of the axes (and it's number) and possibly it's limits.

        Args:
            obs (str, List[str,...]):
            limits ():
            name (str):
        """

        # self._has_rect_limits = True

        if name is None:
            name = "space"
        super().__init__(obs=obs, axes=axes, name=name)
        limits_dict = self._check_convert_input_limits(limit=limits, rect_limits=rect_limits, obs=self.obs,
                                                       axes=self.axes, n_obs=self.n_obs)
        self._limits_dict = limits_dict

    @property
    def has_rect_limits(self) -> bool:
        return all(limit.has_rect_limits for limit in list(self._limits_dict.values())[0].values())

    @classmethod
    @deprecated(date=None, instructions="Use directly the class to create a Space")
    def from_axes(cls, axes: ztyping.AxesTypeInput,
                  limits: Optional[ztyping.LimitsTypeInput] = None, rect_limits=None,
                  name: str = None) -> "zfit.Space":
        """Create a space from `axes` instead of from `obs`.

        Args:
            axes ():
            limits ():
            name (str):

        Returns:
            :py:class:`~zfit.Space`
        """
        # TODO(v0.5):
        # raise BreakingAPIChangeError("from_axes is not needed anymore, create a Space directly.")
        axes = convert_to_container(value=axes, container=tuple)
        if axes is None:
            raise AxesNotSpecifiedError("Axes cannot be `None`")
        new_space = cls(axes=axes, limits=limits, rect_limits=rect_limits, name=name)
        return new_space

    # def _check_set_limits(self, limits: ztyping.LimitsTypeInput):
    #     raise NotImplementedError
    #
    # if limits is not None and limits is not False:
    #     lower, upper = limits
    #     limits = self._check_convert_input_lower_upper(lower=lower, upper=upper)
    # self._limits = limits

    # def _check_convert_input_lower_upper(self, lower, upper):  # Remove?
    #     raise NotImplementedError
    # lower = self._check_convert_input_limit(limit=lower)
    # upper = self._check_convert_input_limit(limit=upper)
    # lower_is_iterable = lower is not None or lower is not False
    # upper_is_iterable = upper is not None or upper is not False
    # if not (lower_is_iterable or upper_is_iterable) and lower is not upper:
    #     ValueError("Lower and upper limits wrong:"
    #                "\nlower = {lower}"
    #                "\nupper = {upper}".format(lower=lower, upper=upper))
    # if lower_is_iterable ^ upper_is_iterable:
    #     raise ValueError("Lower and upper limits wrong:"
    #                      "\nlower = {l}"
    #                      "\nupper = {u}".format(l=lower, u=upper))
    # if lower_is_iterable and upper_is_iterable:
    #     if not shape_np_tf(lower) == shape_np_tf(upper) or (
    #         len(shape_np_tf(lower)) not in (2, 3)):  # 3 for EventSpace eager
    #         raise ValueError("Lower and/or upper limits invalid:"
    #                          "\nlower: {lower}"
    #                          "\nupper: {upper}".format(lower=lower, upper=upper))
    #
    #     if not shape_np_tf(lower)[1] == self.n_obs:
    #         raise ValueError("Limits shape not compatible with number of obs/axes"
    #                          "\nlower: {lower}"
    #                          "\nupper: {upper}"
    #                          "\nn_obs: {n_obs}".format(lower=lower, upper=upper, n_obs=self.n_obs))
    # return lower, upper

    def _check_convert_input_limits(self, limit: Union[ztyping.LowerTypeInput, ztyping.UpperTypeInput],
                                    rect_limits, obs, axes, n_obs,
                                    replace=None) -> Union[ztyping.LowerTypeReturn, ztyping.UpperTypeReturn]:
        """Check and sanitize the input limits as well as the rectangular limits.

        Args:
            limit ():

        Returns:
            dict(obs/axes: ZfitLimit): Limits dictionary containing the observables and/or the axes as a key matching
                `ZfitLimits` objects.

        """
        limits_dict = defaultdict(dict)
        if not isinstance(limit, dict):
            if not isinstance(rect_limits, dict):
                limit = Limit(limit_fn=limit, rect_limits=rect_limits, n_obs=n_obs)
                i_old = 0
                for lim in limit:  # split into smaller ones if possible
                    i = i_old + lim.n_obs
                    if obs is not None:
                        limits_dict['obs'][obs[i_old:i]] = lim
                    if axes is not None:
                        limits_dict['axes'][axes[i_old:i]] = lim
                    i_old = i
            else:
                limits_dict = rect_limits
        else:
            limits_dict = limit

        # TODO: extend input processing
        return limits_dict

        # replace = {} if replace is None else replace
        # if limit is NOT_SPECIFIED or limit is None:
        #     return None
        # if (isinstance(limit, tuple) and limit == ()) or (isinstance(limit, np.ndarray) and limit.size == 0):
        #     raise ValueError("Currently, () is not supported as limits. Should this be default for None?")
        # shape = shape_np_tf(limit)
        # if shape == ():
        #     limit = ((limit,),)
        #
        # shape = shape_np_tf(limit[0])
        # if shape == ():
        #     raise ValueError("Shape of limit {} wrong.".format(limit))
        #
        # # replace
        # if replace:
        #     limit = tuple(tuple(replace.get(l, l) for l in lim) for lim in limit)
        #
        # return limit

    def _extract_limits(self, obs=None, axes=None):
        if (obs is None) and (axes is None):
            raise ValueError("Need to specify at least one, obs or axes.")
        elif (obs is not None) and (axes is not None):
            axes = None  # obs has precedency
        if obs is None:
            obs_in_use = False
            coords_to_extract = axes
        else:
            obs_in_use = True
            coords_to_extract = obs
        coords_to_extract = convert_to_container(coords_to_extract)
        coords_to_extract = set(coords_to_extract)

        limits_to_eval = {}
        limit_dict = self._limits_dict['obs' if obs_in_use else 'axes'].items()
        keys_sorted = sorted(limit_dict, key=lambda x: len(x[0]), reverse=True)
        for key_coords, limit in keys_sorted:
            coord_intersec = frozenset(key_coords).intersection(coords_to_extract)
            if not coord_intersec:  # this limit does not contain any requested obs
                continue
            if coord_intersec == frozenset(key_coords):
                limits_to_eval[key_coords] = limit
            else:
                coord_limit = [coord for coord in key_coords if coord in coord_intersec]
                kwargs = {'obs' if obs_in_use else 'axes': coord_limit}
                try:
                    sublimit = limit.get_subspace(**kwargs)
                except InvalidLimitSubspaceError:
                    raise InvalidLimitSubspaceError(f"Cannot extract {coord_intersec} from limit {limit}.")
                sublimit_coord = limit.obs if obs_in_use else limit.axes
                limits_to_eval[sublimit_coord] = sublimit
                coords_to_extract -= coord_intersec
        return limits_to_eval

    @property
    @deprecated(date=None, instructions="`limits` is depreceated (currently) due to the unambiguous nature of the word."
                                        " Use `inside` to check if an Tensor is inside the limits or"
                                        " `rect_limits` if you need to retreave the rectangular limits.")
    def limits(self) -> ztyping.LimitsTypeReturn:
        """Return the limits.

        Returns:

        """
        return self.rect_limits

    @property
    def rect_limits(self) -> ztyping.LimitsTypeReturn:
        """Return the limits.

        Returns:

        """
        # self._check_has_limits
        if not self.has_rect_limits:
            return False
        lower_ordered, upper_ordered = self._rect_limits_z()
        rect_limits = z.convert_to_tensor(lower_ordered), z.convert_to_tensor(upper_ordered)
        return rect_limits

    @property
    def _rect_limits_np(self):
        """Return the rectangular limits as `np.ndarray`. Raises error if not possible.

        Returns:
            (lower, upper):

        Raises:
            CannotConvertToNumpyError: In case the conversion fails.
        """
        lower, upper = self._rect_limits_z()

        lower = z.unstable._try_convert_numpy(lower)
        upper = z.unstable._try_convert_numpy(upper)
        return (lower, upper)

    def _rect_limits_z(self):
        limits_obs = []
        rect_lower_unordered = []
        rect_upper_unordered = []
        obs_in_use = self.obs is not None
        limits_dict = self._limits_dict['obs' if obs_in_use else 'axes']
        for obs_limit, limit in limits_dict.items():  # TODO: what about axis?
            if obs_in_use ^ isinstance(obs_limit[0], str):  # testing first element is sufficient
                continue  # skipping if stored in different type of coords
            limits_obs.extend(obs_limit)
            lower, upper = limit.rect_limits
            rect_lower_unordered.append(lower)
            rect_upper_unordered.append(upper)
        reorder_kwargs = {'x_obs' if obs_in_use else 'x_axes': limits_obs}
        lower_stacked = z.unstable.concat(rect_lower_unordered,
                                          axis=-1)  # TODO: improve this layer, is list does not recognize it as tensor?
        lower_ordered = self.reorder_x(lower_stacked, **reorder_kwargs)
        upper_stacked = z.unstable.concat(rect_upper_unordered,
                                          axis=-1)  # TODO: improve this layer, is list does not recognize it as tensor?
        upper_ordered = self.reorder_x(upper_stacked, **reorder_kwargs)
        return lower_ordered, upper_ordered

    def reorder_x(self, x, x_obs=None, x_axes=None, func_obs=None, func_axes=None):
        return self.coords.reorder_x(x=x, x_obs=x_obs, x_axes=x_axes, func_obs=func_obs, func_axes=func_axes)

    def rect_area(self) -> float:
        return calculate_rect_area(rect_limits=self.rect_limits)

    @property
    def has_limits(self):
        return (not self.limits_not_set) and self.has_rect_limits is not False

    @property
    def limits_not_set(self):
        return len(self._limits_dict) == 0

    @property
    def limits_are_set(self):
        return not self.limits_not_set

    @property
    def limits_are_false(self):
        return self.limits_are_set and not self.has_limits

    @property
    def limit2d(self) -> Tuple[float, float, float, float]:
        """Simplified `limits` for exactly 2 obs, 1 limit: return the tuple(low_obs1, low_obs2, up_obs1, up_obs2).

        Returns:
            tuple(float, float, float, float): so `low_x, low_y, up_x, up_y = space.limit2d` for a single, 2 obs limit.
                low_x is the lower limit in x, up_x is the upper limit in x etc.

        Raises:
            RuntimeError: if the conditions (n_obs or n_limits) are not satisfied.
        """
        raise BreakingAPIChangeError("This function is gone TODO alternative to use?")

    @property
    def limits1d(self) -> Tuple[float]:
        """Simplified `.limits` for exactly 1 obs, n limits: return the tuple(low_1, ..., low_n, up_1, ..., up_n).

        Returns:
            tuple(float, float, ...): so `low_1, low_2, up_1, up_2 = space.limits1d` for several, 1 obs limits.
                low_1 to up_1 is the first interval, low_2 to up_2 is the second interval etc.

        Raises:
            RuntimeError: if the conditions (n_obs or n_limits) are not satisfied.
        """
        return self.rect_limits
        # raise BreakingAPIChangeError("This function is gone TODO alternative to use?")

    @property
    @deprecated(date=None, instructions="Depreceated (currently) due to the unambiguous nature of the word."
                                        " Use `rect_lower` instead.")
    def lower(self) -> ztyping.LowerTypeReturn:
        """Return the lower limits.

        Returns:

        """
        return self.rect_lower
        # raise BreakingAPIChangeError("Use rect_lower")

    @property
    def rect_lower(self) -> ztyping.LowerTypeReturn:
        """Return the lower limits.

        Returns:

        """
        return self.rect_limits[0]

    @property
    @deprecated(date=None, instructions="depreceated (currently) due to the unambiguous nature of the word."
                                        " Use `rect_upper` instead.")
    def upper(self) -> ztyping.UpperTypeReturn:
        """Return the upper limits.

        Returns:

        """
        return self.rect_upper

    @property
    def rect_upper(self) -> ztyping.UpperTypeReturn:
        """Return the upper limits.

        Returns:

        """
        return self.rect_limits[1]

    @property
    def n_limits(self) -> int:
        """The number of different limits.

        Returns:
            int >= 1
        """
        return len(tuple(self))

    @property
    @deprecated(date=None, instructions="Iterate over the space directly and"
                                        " use the limits from the spaces.")
    def iter_limits(self, as_tuple: bool = True) -> ztyping._IterLimitsTypeReturn:
        """Return the limits, either as :py:class:`~zfit.Space` objects or as pure limits-tuple.

        This makes iterating over limits easier: `for limit in space.iter_limits()`
        allows to, for example, pass `limit` to a function that can deal with simple limits
        only or if `as_tuple` is True the `limit` can be directly used to calculate something.

        Example:
            .. code:: python

                for lower, upper in space.iter_limits(as_tuple=True):
                    integrals = integrate(lower, upper)  # calculate integral
                integral = sum(integrals)


        Returns:
            List[:py:class:`~zfit.Space`] or List[limit,...]:
        """
        # TODO: soften?
        raise BreakingAPIChangeError
        if not self.has_limits:
            raise LimitsNotSpecifiedError("Space does not have limits, cannot iterate over them.")
        if as_tuple:
            return tuple(zip(self.lower, self.upper))
        else:
            space_objects = []
            for lower, upper in self.iter_limits(as_tuple=True):
                if not (lower is None or lower is False):
                    lower = (lower,)
                    upper = (upper,)
                    limit = lower, upper
                else:
                    limit = lower
                space = type(self)(obs=self.obs, axes=self.axes, limits=limit)
                space_objects.append(space)
            return tuple(space_objects)

    def with_limits(self, limits: ztyping.LimitsTypeInput = None, rect_limits=None,
                    name: Optional[str] = None) -> ZfitSpace:
        """Return a copy of the space with the new `limits` (and the new `name`).

        Args:
            limits ():
            name (str):

        Returns:
            :py:class:`~zfit.Space`
        """
        # self._check_convert_input_limits(limits=limits, rect_limits=rect_limits, obs=self.obs, axes=self.axes,
        #                                  n_obs=self.n_obs)
        # new_space = self.copy(limits=limits, rect_limits=rect_limits, name=name)
        new_space = type(self)(obs=self.coords, limits=limits, rect_limits=rect_limits)
        return new_space

    def with_obs(self, obs: Optional[ztyping.ObsTypeInput], allow_superset: bool = False,
                 allow_subset: bool = False) -> "zfit.Space":
        """Sort by `obs` and return the new instance.

        Args:
            obs ():
            allow_superset (bool): Allow `axes` to be a superset of the `Spaces` axes
            allow_subset (bool): Allow `axes` to be a subset of the `Spaces` axes

        Returns:
            :py:class:`~zfit.Space`
        """
        # TODO: remove chekcs, move to coords?
        if obs is None:  # drop obs, check if there are axes
            if self.obs is None:
                return self
            if self.axes is None:
                raise AxesIncompatibleError("cannot remove obs (using None) for a Space without axes")
            new_limits = self._limits_dict['obs']
            new_space = self.copy(obs=obs, limits=new_limits)
        else:
            obs = _convert_obs_to_str(obs)
            coords = self.coords.with_obs(obs, allow_superset=allow_superset, allow_subset=allow_subset)
            new_space = type(self)(coords, limits=self._limits_dict)
            # new_indices = self.get_reorder_indices(obs=obs)
            # new_space = self.with_indices(indices=new_indices)
        return new_space

    def with_axes(self, axes: Optional[ztyping.AxesTypeInput], allow_superset: bool = False,
                  allow_subset: bool = False) -> "zfit.Space":
        """Sort by `axes` and return the new instance. `None` drops the axes.

        Args:
            axes ():
            allow_superset (bool): Allow `axes` to be a superset of the `Spaces` axes

        Returns:
            :py:class:`~zfit.Space`
        """
        # TODO: remove chekcs, move to coords?
        if axes is None:  # drop axes
            if self.axes is None:
                return self
            if self.obs is None:
                raise ObsIncompatibleError("Cannot remove axes (using None) for a Space without obs")
            new_limits = self._limits_dict['axes']
            new_space = self.copy(axes=axes, limits=new_limits)
        else:
            axes = convert_to_axes(axes)
            if self.axes is None:
                if not len(axes) == len(self.obs):
                    raise AxesIncompatibleError(f"Trying to set axes {axes} to object with obs {self.obs}")
                new_limits = self._limits_dict['obs'].copy()
                for obs, limit in self._limits_dict['obs']:
                    ax = tuple(axes[obs.index[ob]] for ob in obs)
                    new_limits['axes'][ax] = limit
            else:

                coords = self.coords.with_axes(axes=axes, allow_superset=allow_superset, allow_subset=allow_subset)
                new_space = type(self)(coords, limits=self._limits_dict)
                # new_indices = self.get_reorder_indices(axes=axes)
                # new_space = self.with_indices(indices=new_indices)

        return new_space

    def get_reorder_indices(self, obs: ztyping.ObsTypeInput = None,
                            axes: ztyping.AxesTypeInput = None) -> Tuple[int]:
        """Indices that would order `self.obs` as `obs` respectively `self.axes` as `axes`.

        Args:
            obs ():
            axes ():

        Returns:

        """
        obs_none = obs is None
        axes_none = axes is None

        obs_is_defined = self.obs is not None and not obs_none
        axes_is_defined = self.axes is not None and not axes_none
        if not (obs_is_defined or axes_is_defined):
            raise ValueError(
                "Neither the `obs` (argument and on instance) nor `axes` (argument and on instance) are defined.")

        if obs_is_defined:
            old, new = self.obs, [o for o in obs if o in self.obs]
        else:
            old, new = self.axes, [a for a in axes if a in self.axes]

        new_indices = _reorder_indices(old=old, new=new)
        return new_indices

    def get_obs_axes(self, obs: ztyping.ObsTypeInput = None, axes: ztyping.AxesTypeInput = None):
        raise BreakingAPIChangeError("Simply get the coords if needed?")

    @property
    def obs_axes(self):
        # TODO(Mayou36): what if axes is None?
        raise BreakingAPIChangeError
        # return OrderedDict((o, ax) for o, ax in zip(self.obs, self.axes))

    def with_coords(self, coords: ZfitOrderableDimensional, allow_superset=False, allow_subset=True) -> "zfit.Space":
        """Return a new :py:class:`~zfit.Space` with reordered observables and set the `axes`.


        Args:
            coords (OrderedDict[str, int]): An ordered dict with {obs: axes}.
            ordered (bool): If True (and the `obs_axes` is an `OrderedDict`), the
            allow_subset ():

        Returns:
            :py:class:`~zfit.Space`:
        """
        new_space_obs = None
        new_space_axes = None
        if self.obs is not None and coords.obs is not None:
            new_space_obs = self.with_obs(coords.obs, allow_superset=allow_superset, allow_subset=allow_subset)

        if self.axes is not None and coords.axes is not None:
            new_space_axes = self.with_axes(coords.axes, allow_superset=allow_superset, allow_subset=allow_subset)

        if new_space_obs is not None and new_space_axes is not None:
            if new_space_obs.axes != new_space_axes.axes or new_space_obs.obs != new_space_axes.obs:
                raise CoordinatesIncompatibleError(f"Cannot use Coordinates {coords} to get a subspace of {self}"
                                                   f" because the obs and axes assignement does not agree."
                                                   f" The ordering of the axes with respect to the obs"
                                                   f" is different.")

        new_space = new_space_obs if new_space_axes is None else new_space_axes
        if new_space is None:
            raise CoordinatesIncompatibleError(f"Cannot use Coordinates {coords} to get a subspace of {self}"
                                               f" because neither obs nor axes are specified in both.")

        return new_space

    def with_obs_axes(self, **kwargs):
        raise BreakingAPIChangeError("What is this needed for?")
        # new_space = type(self)._from_any(obs=self.obs, axes=self.axes, limits=self.limits)
        # new_space._set_obs_axes(obs_axes=obs_axes, ordered=ordered, allow_subset=allow_subset)
        # return new_space

    def with_autofill_axes(self, overwrite: bool = False) -> "zfit.Space":
        """Return a :py:class:`~zfit.Space` with filled axes corresponding to range(len(n_obs)).

        Args:
            overwrite (bool): If `self.axes` is not None, replace the axes with the autofilled ones.
                If axes is already set, don't do anything if `overwrite` is False.

        Returns:
            :py:class:`~zfit.Space`
        """
        if self.axes is None or overwrite:
            new_axes = tuple(range(self.n_obs))
            new_space = self.copy(axes=new_axes)
        else:
            new_space = self

        return new_space

    @deprecated(date=None, instructions="Use rect_area to obtain the rectangular area.")
    def area(self) -> float:
        """Return the total area of all the limits and axes. Useful, for example, for MC integration."""
        return self.rect_limits

    def get_subspace(self, obs: ztyping.ObsTypeInput = None, axes: ztyping.AxesTypeInput = None,
                     name: Optional[str] = None) -> "zfit.Space":
        """Create a :py:class:`~zfit.Space` consisting of only a subset of the `obs`/`axes` (only one allowed).

        Args:
            obs (str, Tuple[str]):
            axes (int, Tuple[int]):
            name ():

        Returns:

        """
        if obs is not None and axes is not None:
            raise ValueError("Cannot specify `obs` *and* `axes` to get subspace.")
        if axes is None and obs is None:
            raise ValueError("Either `obs` or `axes` has to be specified and not None")

        # try to use observables to get index
        obs = self._check_convert_input_obs(obs=obs, allow_none=True)
        axes = self._check_convert_input_axes(axes=axes, allow_none=True)
        if obs is not None:
            limits_dict = self._extract_limits(obs=obs)
            new_coords = self.coords.with_obs(obs, allow_subset=True)
        else:
            limits_dict = self._extract_limits(axes=axes)
            new_coords = self.coords.with_axes(axes=axes, allow_subset=True)
        new_space = type(self)(obs=new_coords, limits=limits_dict)
        return new_space

    def copy(self, **overwrite_kwargs) -> "zfit.Space":
        """Create a new :py:class:`~zfit.Space` using the current attributes and overwriting with
        `overwrite_overwrite_kwargs`.

        Args:
            name (str): The new name. If not given, the new instance will be named the same as the
                current one.
            **overwrite_kwargs ():

        Returns:
            :py:class:`~zfit.Space`
        """
        kwargs = {'name': self.name,
                  'limits': self._limits_dict,
                  'axes': self.axes,
                  'obs': self.obs}
        kwargs.update(overwrite_kwargs)
        if set(overwrite_kwargs) - set(kwargs):
            raise KeyError("Not usable keys in `overwrite_kwargs`: {}".format(set(overwrite_kwargs) - set(kwargs)))

        new_space = type(self)(**kwargs)
        return new_space

    # Operators

    @property
    def has_rect_limits(self):
        if self.obs is not None:
            limits_dict = self._limits_dict.get('obs')
        else:
            limits_dict = self._limits_dict.get('axes')
        if not limits_dict:
            return False
        rect_limits = [limit.has_rect_limits for limit in limits_dict.values()]
        all_rect_limits = all(rect_limits)
        return all_rect_limits and len(rect_limits) > 0

    def _inside(self, x, guarantee_limits):  # TODO: add proper implementation, as with rect_limits
        xs_inside = []
        obs_in_use = self.obs is not None
        limits_dict = self._limits_dict['obs' if obs_in_use else 'axes']
        for coords, limit in limits_dict.items():
            reorder_kwargs = {'func_obs' if obs_in_use else 'func_axes': coords}
            x_sub = self.reorder_x(x, **reorder_kwargs)
            x_inside = limit.inside(x_sub)
            # reorder_back_kwargs = {'x_obs' if obs_in_use else 'x_axes': coords}
            # x_sub_reordered = self.reorder_x(x_inside, **reorder_back_kwargs)
            xs_inside.append(x_inside)
        all_inside = tf.reduce_all(xs_inside, axis=0)
        return all_inside

    @property
    @deprecated(date=None, instructions="depreceated, use `rect_limits` instead which has a similar functionality"
                                        " Use `inside` to check if an Tensor is inside the limits.")
    def limit1d(self) -> Tuple[float, float]:
        """Simplified limits getter for 1 obs, 1 limit only: return the tuple(lower, upper).

        Returns:
            tuple(float, float): so :code:`lower, upper = space.limit1d` for a simple, 1 obs limit.

        Raises:
            RuntimeError: if the conditions (n_obs or n_limits) are not satisfied.
        """
        if self.n_obs > 1:
            raise RuntimeError("Cannot call `limit1d, as `Space` has more than one observables: {}".format(self.n_obs))
        if self.n_limits > 1:
            raise RuntimeError("Cannot call `limit1d, as `Space` has several limits: {}".format(self.n_limits))
        return self.rect_limits


def add_spaces_new(*spaces: Iterable["ZfitSpace"], name=None):
    """Add two spaces and merge their limits if possible or return False.

    Args:
        spaces (Iterable[:py:class:`~zfit.Space`]):

    Returns:
        Union[None, :py:class:`~zfit.Space`, bool]:

    Raises:
        LimitsIncompatibleError: if limits of the `spaces` cannot be merged because they overlap
    """
    # spaces = convert_to_container(spaces)
    if not all(isinstance(space, ZfitSpace) for space in spaces):
        raise TypeError(f"Can only add type ZfitSpace, not {spaces}")
    return MultiSpace(spaces, name=name)


# WORKHERE


# def create_rect_limits_func(rect_limits):
#     def inside(x: tf.Tensor):
#         if not x.shape.ndims > 1:
#             raise ValueError("x has ndims <= 1, which is most probably not wanted. The default shape for array-like"
#                              " structures is (nevents, n_obs).")
#         lower, upper = rect_limits
#         below_upper = tf.reduce_all(input_tensor=tf.less_equal(x, upper), axis=-1)  # if all obs inside
#         above_lower = tf.reduce_all(input_tensor=tf.greater_equal(x, lower), axis=-1)
#         inside = tf.logical_and(above_lower, below_upper)
#         return inside

def get_coord(space, obs_in_use=True):
    if obs_in_use:
        return space.obs
    else:
        return space.axes


def combine_spaces_new(*spaces: Iterable[Space]):
    """Combine spaces with different `obs` and `limits` to one `space`.

    Checks if the limits in each obs coincide *exactly*. If this is not the case, the combination
    is not unambiguous and `False` is returned

    Args:
        spaces (List[:py:class:`~zfit.Space`]):

    Returns:
        `zfit.Space` or False: Returns False if the limits don't coincide in one or more obs. Otherwise
            return the :py:class:`~zfit.Space` with all obs from `spaces` sorted by the order of `spaces` and with the
            combined limits.
    Raises:
        ValueError: if only one space is given
        LimitsIncompatibleError: If the limits of one or more spaces (or within a space) overlap
        LimitsNotSpecifiedError: If the limits for one or more obs but not all are None.
    """
    spaces = convert_to_container(spaces, container=tuple)
    # if len(spaces) <= 1:
    #     return spaces
    # raise ValueError("Need at least two spaces to test limit consistency.")  # TODO: allow? usecase?

    all_obs = common_obs(spaces=spaces)
    all_axes = common_axes(spaces=spaces)
    using_obs = bool(all_obs)
    all_coords = all_obs if using_obs else all_axes
    if using_obs:
        spaces = tuple(space.with_obs(all_obs, allow_superset=True) for space in spaces)
    elif all_axes:
        spaces = tuple(space.with_axes(all_axes, allow_superset=True) for space in spaces)
    else:
        raise CoordinatesUnderdefinedError("Neither `obs` nor `axes` exist in all spaces.")

    all_limits_false = all([space.limits_are_false for space in spaces])
    all_limits_not_set = all([space.limits_not_set for space in spaces])
    has_limits = [space.has_limits for space in spaces]
    # limits_are_set = [space. for space in spaces]
    if all_limits_false:
        limits = False
    elif all_limits_not_set:
        limits = None
    elif not all(has_limits):
        raise LimitsIncompatibleError("Limits either have to be set, not set, or False for all spaces to be combined.")
    else:
        # TODO: how to handle multispaces?
        limits_dict = {}
        for coord in all_coords:
            space_with_coord = [space for space in spaces if coord in get_coord(space, using_obs)]
            if any(isinstance(space, MultiSpace) for space in space_with_coord):
                raise WorkInProgressError("Multispace combination is not yet implemented. Work in progress.")
            assert space_with_coord, "empty, cannot be. This is a bug."
            limits_coord = []
            for space in space_with_coord:
                if type(space) == Space:  # has to be the exact type, we use an implementation detail here
                    limits_coord.append(space._extract_limits(obs=coord if using_obs else None,
                                                              axes=coord if not using_obs else None)
                                        for space in space_with_coord)
                else:
                    limits_coord.append(space.with_obs(obs=coord) if using_obs else space.with_axes(axes=coord)
                                        for space in space_with_coord)
            any_non_equal = any([limits_coord[0] != limit for limit in limits_coord[1:]])
            if any_non_equal:
                raise LimitsIncompatibleError(f"Limits in coord {coord} do not match for spaces {limits_coord}")
            limits_dict[coord] = limits_coord[0]

        limits = {'obs' if using_obs else 'axes': limits_dict}

    # all_lower = []
    # all_upper = []
    #
    # # create the lower and upper limits with all obs replacing missing dims with None
    # # With this, all limits have the same length
    # # TODO?
    # # if limits_overlap(spaces=spaces, allow_exact_match=True):
    # #     raise LimitsIncompatibleError("Limits overlap")
    #
    # for space in flatten_spaces(spaces):
    #     if space.limits is None:
    #         continue
    #     lowers, uppers = space.limits
    #     lower = [tuple(low[space.obs.index(ob)] for low in lowers) if ob in space.obs else None for ob in all_obs]
    #     upper = [tuple(up[space.obs.index(ob)] for up in uppers) if ob in space.obs else None for ob in all_obs]
    #     all_lower.append(lower)
    #     all_upper.append(upper)
    #
    # def check_extract_limits(limits_spaces):
    #     new_limits = []
    #
    #     if not limits_spaces:
    #         return None
    #     for index, obs in enumerate(all_obs):
    #         current_limit = None
    #         for limit in limits_spaces:
    #             lim = limit[index]
    #
    #             if lim is not None:
    #                 if current_limit is None:
    #                     current_limit = lim
    #                 elif not np.allclose(current_limit, lim):
    #                     return False
    #         else:
    #             if current_limit is None:
    #                 raise LimitsNotSpecifiedError("Limits in obs {} are not specified".format(obs))
    #             new_limits.append(current_limit)
    #
    #     n_limits = int(np.prod(tuple(len(lim) for lim in new_limits)))
    #     new_limits_comb = [[] for _ in range(n_limits)]
    #     for limit in new_limits:
    #         for lim in limit:
    #             for i in range(int(n_limits / len(limit))):
    #                 new_limits_comb[i].append(lim)
    #
    #     new_limits = tuple(tuple(limit) for limit in new_limits_comb)
    #     return new_limits

    # new_lower = check_extract_limits(all_lower)
    # new_upper = check_extract_limits(all_upper)
    # assert not (new_lower is None) ^ (new_upper is None), "Bug, please report issue. either both are defined or None."
    # if new_lower is None:
    #     limits = None
    # elif new_lower is False:
    #     return False
    # else:
    #     limits = (new_lower, new_upper)
    new_space = Space(obs=all_obs if using_obs else None, axes=all_axes if all_axes else None, limits=limits)
    # if new_space.n_limits > 1:
    #     new_space = MultiSpace(Space, obs=all_obs)
    return new_space


def less_equal_space(space1, space2, allow_graph=True):
    return compare_multispace(space1=space1, space2=space2,
                              comparator=lambda limit1, limit2: limit1.less_equal(limit2, allow_graph=allow_graph))


def equal_space(space1, space2, allow_graph=True):
    return compare_multispace(space1=space1, space2=space2,
                              comparator=lambda limit1, limit2: limit1.equal(limit2, allow_graph=allow_graph))


def compare_multispace(space1: ZfitSpace, space2: ZfitSpace, comparator: Callable):
    """Compare multiple spaces if they have the same obs, axes, and, if a comparator is given, limits.

    It is automatically checked if the limits are set resp. are False

    Args:
        space1:
        space2:
        comparator:

    Returns:

    """
    axes_not_none = space1.axes is not None and space2.axes is not None
    obs_not_none = space1.obs is not None and space2.obs is not None
    if not (axes_not_none or obs_not_none):  # if both are None
        return False
    if axes_not_none:
        if set(space1.axes) != set(space2.axes):
            return False
    if obs_not_none:
        if set(space1.obs) != set(space2.obs):
            return False
    # check limits
    if space1.limits is None:
        if space2.limits is None:
            return True
        else:
            return False

    elif space1.limits is False:
        if space2.limits is False:
            return True
        else:
            return False

    return compare_limits_multispace(space1, space2, comparator=comparator)


def compare_limits_multispace(space1: ZfitSpace, space2: ZfitSpace, comparator: Callable) -> bool:
    if not len(space1) == len(space2):
        return False
    space2_reordered = space2.with_coords(space1)

    spaces_to_check2 = list(space2_reordered)
    spaces_to_check1 = list(space1)
    for index1, space11 in enumerate(spaces_to_check1):
        limit_is_le = False
        for index2, space22 in enumerate(spaces_to_check2):
            # each entry *has to* match the entry of the other limit, otherwise it's not the same

            axis_pos_comp = compare_limits_coords_dict(space11._limits_dict, space22._limits_dic, comparator=comparator)
            if axis_pos_comp:  # if not the same, don't test other dims
                spaces_to_check2.pop(index1)
                spaces_to_check1.pop(index2)
                break
            else:
                limit_is_le = False  # no break -> all axes coincide
        if not limit_is_le:  # for this `limit`, no other_limit matched
            return False
    return True


def compare_limits_coords_dict(limits1: Mapping[str, Mapping[Iterable, ZfitLimit]],
                               limits2: Mapping[str, Mapping[Iterable, ZfitLimit]],
                               comparator: Callable,
                               require_all_coord_types: bool = False) -> bool:
    if not limits1.keys() == limits2.keys() and require_all_coord_types:
        return False
    equal = False
    for coord_type, limit1_dict in limits1.items():
        limit2_dict = limits2.get(coord_type)
        if limit2_dict is None:
            continue
        equal = compare_limits_dict(limit1_dict, limit2_dict, comparator=comparator)
        if equal is False:
            break
    return equal


def compare_limits_dict(dict1: Mapping, dict2: Mapping, comparator: Callable) -> bool:
    for coord, limit1 in dict1.items():
        limit2 = dict2.get(coord)
        if limit2 is None or not comparator(limit1, limit2):
            return False
    return True


def _convert_axes_to_int(axes):
    if isinstance(axes, ZfitSpace):
        axes = axes.axes
    else:
        axes = convert_to_container(axes, container=tuple)
    return axes


def _convert_obs_to_str(obs):
    if isinstance(obs, ZfitSpace):
        obs = obs.obs
    else:
        obs = convert_to_container(obs, container=tuple)
    return obs


def flatten_spaces(spaces):
    return tuple(s for space in spaces for s in space)


class MultiSpace(BaseSpace):

    def __new__(cls, spaces: Iterable[ZfitSpace], obs=None, axes=None, name: str = None) -> Any:
        spaces, obs, axes = cls._check_convert_input_spaces_obs_axes(spaces, obs, axes)
        if len(spaces) == 1:
            return spaces[0]
        space = super().__new__(cls)
        space._tmp_store_spaces_obs_axes = spaces, obs, axes
        return space

    def __init__(self, spaces: Iterable[ZfitSpace], obs=None, axes=None, name: str = None) -> None:
        del spaces, obs, axes  # not needed, we take the already preprocessed.
        spaces, obs, axes = self._tmp_store_spaces_obs_axes
        del self._tmp_store_spaces_obs_axes
        if name is None:
            name = "MultiSpace"
        super().__init__(obs, axes, name)
        self.spaces = spaces

    @staticmethod
    def _initialize_space(space, spaces, obs, axes):
        space._obs = obs
        space._axes = axes
        space.spaces = spaces
        return space

    @staticmethod
    def _check_convert_input_spaces_obs_axes(spaces, obs, axes):  # TODO: do something with axes
        spaces = flatten_spaces(spaces)
        all_have_obs = all(space.obs is not None for space in spaces)
        all_have_axes = all(space.axes is not None for space in spaces)
        if all_have_axes:
            axes = spaces[0].axes if axes is None else convert_to_axes(axes)

        if all_have_obs:
            obs = spaces[0].obs if obs is None else convert_to_obs_str(obs)
            spaces = [space.with_obs(obs) for space in spaces]
            if not (all_have_axes and all(space.axes == axes for space in spaces)):  # obs coincide, axes don't -> drop
                spaces = [space.with_axes(None) for space in spaces]
        elif all_have_axes:
            if all(space.obs is None for space in spaces):
                spaces = [space.with_axes(axes) for space in spaces]

        else:
            raise SpaceIncompatibleError("Spaces do not have consistent obs and/or axes.")

        if all(space.has_limits for space in spaces):
            # check overlap, reduce common limits
            pass
        elif not any(space.has_limits for space in spaces):
            spaces = [spaces[0]]  # if all are None, then nothing to add
        else:  # some have limits, some don't -> does not really make sense (or just drop the ones without limits?)
            raise LimitsIncompatibleError(
                "Some spaces have limits, other don't. This behavior may change in the future "
                "to allow spaces with None to be simply ignored.\n"
                "If you prefer this behavior, please open an issue on github.")

        spaces = tuple(spaces)

        # if axes is not None:
        #     raise WorkInProgressError("Axes not yet implemented")
        # spaces = convert_to_container(spaces, container=tuple)
        # spaces = flatten_spaces(spaces)
        # if obs is None:
        #     obs = spaces[0].obs
        # else:
        #     obs = convert_to_obs_str(obs)
        #
        # all_have_obs = all(space.obs is not None for space in spaces)
        # if all_have_obs:
        #     if not all(frozenset(obs) == frozenset(space.obs) for space in spaces):
        #         raise ObsIncompatibleError(f"observables of spaces do not coincide: {spaces}")
        #     spaces = tuple(space.with_obs(obs) for space in spaces)
        # else:
        #     if any(space.obs is not None for space in spaces):
        #         raise ObsIncompatibleError("Some spaces have obs, other don't")  # TODO, better check
        #
        # if axes is None:
        #     axes = spaces[0].axes
        # else:
        #     axes = convert_to_axes(axes)
        #
        # all_have_axes = all(space.axes is not None for space in spaces)
        # if all_have_axes:
        #     if not all(frozenset(axes) == frozenset(space.axes) for space in spaces):
        #         raise AxesIncompatibleError(f"observables of spaces do not coincide: {spaces}")
        #     spaces = tuple(space.with_axes(axes) for space in spaces)
        #
        # else:
        #     if any(space.axes is not None for space in spaces):
        #         raise AxesIncompatibleError("Some spaces have axes, others don't")  # TODO, better check
        return spaces, obs, axes

    @property
    def has_rect_limits(self) -> bool:
        return all(space.has_rect_limits for space in self.spaces)

    # @property
    # def obs(self) -> ztyping.ObsTypeReturn:
    #     """The observables ("axes with str")the space is defined in.
    #
    #     Returns:
    #
    #     """
    #     return self._obs
    #
    # @property
    # def axes(self) -> ztyping.AxesTypeReturn:
    #     """The axes ("obs with int") the space is defined in.
    #
    #     Returns:
    #
    #     """
    #     return self._axes

    # noinspection PyPropertyDefinition
    @property
    def limits(self) -> None:
        if all(space.limits is None for space in self):
            return None
        self._raise_limits_not_implemented()

    @property
    def has_limits(self):
        try:
            return (not self.limits_not_set) and self.limits is not False
        except MultipleLimitsNotImplementedError:
            return True

    @property
    def limits_not_set(self):
        try:
            return self.limits is None
        except MultipleLimitsNotImplementedError:
            return False

    # noinspection PyPropertyDefinition
    @property
    def lower(self) -> None:
        if all(space.lower is None for space in self):
            return None
        self._raise_limits_not_implemented()

    # noinspection PyPropertyDefinition
    @property
    def upper(self) -> None:
        if all(space.upper is None for space in self):
            return None
        self._raise_limits_not_implemented()

    def with_limits(self, limits, name):
        self._raise_limits_not_implemented()

    @deprecated(date=None, instructions="Use rect_area to obtain the rectangular area of the space.")
    def area(self) -> float:
        return self.rect_area()

    def rect_area(self) -> float:
        return z.reduce_sum([space.rect_area() for space in self], axis=0)

    def with_obs(self, obs, allow_superset: bool = False, allow_subset: bool = True):
        spaces = [space.with_obs(obs, allow_superset=allow_superset, allow_subset=allow_subset)
                  for space in self.spaces]
        return type(self)(spaces, obs=obs)

    def with_axes(self, axes, allow_superset: bool = False, allow_subset: bool = True):
        spaces = [space.with_axes(axes, allow_superset=allow_superset, allow_subset=allow_subset)
                  for space in self.spaces]
        return type(self)(spaces, axes=axes)

    def with_coords(self, coords, allow_superset=False, allow_subset=True):
        new_spaces = [space.with_coords(coords, allow_superset=allow_superset, allow_subset=allow_subset)
                      for space in self]
        return type(self)(spaces=new_spaces)

    def with_autofill_axes(self, overwrite: bool):
        spaces = [space.with_autofill_axes(overwrite) for space in self.spaces]
        return type(self)(spaces)

    def iter_limits(self, as_tuple=True):
        raise BreakingAPIChangeError("This should not be used anymore")

    def iter_areas(self, rel: bool = False) -> Tuple[float, ...]:
        raise BreakingAPIChangeError("This should not be used anymore")

    def get_subspace(self, obs: ztyping.ObsTypeInput = None, axes=None, name=None) -> ZfitSpace:
        spaces = [space.get_subspace(obs=obs, axes=axes) for space in self.spaces]
        return type(self)(spaces, name=name)

    def _raise_limits_not_implemented(self):
        raise MultipleLimitsNotImplementedError(
            "Limits/lower/upper not implemented for MultiSpace. This error is either caught"
            " automatically as part of the codes logic or the MultiLimit case should"
            " now be implemented. To do that, simply iterate through it, works also"
            "for simple spaces.")

    def _inside(self, x, guarantee_limits):
        inside_limits = [space.inside(x, guarantee_limits=guarantee_limits) for space in self]
        inside = tf.reduce_any(input_tensor=inside_limits, axis=0)  # has to be inside one limit
        return inside

    def __iter__(self) -> ZfitSpace:
        yield from self.spaces

    def __eq__(self, other):
        if not isinstance(other, MultiSpace):
            return NotImplemented
        all_equal = frozenset(self) == frozenset(other)
        return all_equal

    def __hash__(self):
        return hash(self.spaces)


#
# class FunctionSpace(BaseSpace):
#
#     def __init__(self, obs=None, axes=None, limit_fn=None, rect_limits=None, name="FunctionSpace"):
#         super().__init__(name, obs=obs, axes=axes, rect_limits=rect_limits, name=name)
#         self.limit_fn = limit_fn
#
#     def _inside(self, x, guarantee_limits):
#         return self.limit_fn(x)
#
#     @property
#     def limits_not_set(self):
#         return self.limit_fn is None
#
#     @property
#     def has_limits(self):
#         return not self.limits_not_set


def convert_to_space(obs: Optional[ztyping.ObsTypeInput] = None, axes: Optional[ztyping.AxesTypeInput] = None,
                     limits: Optional[ztyping.LimitsTypeInput] = None,
                     *, overwrite_limits: bool = False, one_dim_limits_only: bool = True,
                     simple_limits_only: bool = True) -> Union[None, ZfitSpace, bool]:
    """Convert *limits* to a :py:class:`~zfit.Space` object if not already None or False.

    Args:
        obs (Union[Tuple[float, float], :py:class:`~zfit.Space`]):
        limits ():
        axes ():
        overwrite_limits (bool): If `obs` or `axes` is a :py:class:`~zfit.Space` _and_ `limits` are given, return an instance
            of :py:class:`~zfit.Space` with the new limits. If the flag is `False`, the `limits` argument will be
            ignored if
        one_dim_limits_only (bool):
        simple_limits_only (bool):

    Returns:
        Union[:py:class:`~zfit.Space`, False, None]:

    Raises:
        OverdefinedError: if `obs` or `axes` is a :py:class:`~zfit.Space` and `axes` respectively `obs` is not `None`.
    """
    space = None

    # Test if already `Space` and handle
    if isinstance(obs, ZfitSpace):
        if axes is not None:
            raise OverdefinedError("if `obs` is a `Space`, `axes` cannot be defined.")
        space = obs
    elif isinstance(axes, ZfitSpace):
        if obs is not None:
            raise OverdefinedError("if `axes` is a `Space`, `obs` cannot be defined.")
        space = axes
    elif isinstance(limits, ZfitSpace):
        return limits
    if space is not None:
        # set the limits if given
        if limits is not None and (overwrite_limits or space.limits is None):
            if isinstance(limits, ZfitSpace):  # figure out if compatible if limits is `Space`
                if not (limits.obs == space.obs or
                        (limits.axes == space.axes and limits.obs is None and space.obs is None)):
                    raise IntentionNotUnambiguousError(
                        "`obs`/`axes` is a `Space` as well as the `limits`, but the "
                        "obs/axes of them do not match")
                else:
                    limits = limits.limits

            space = space.with_limits(limits=limits)
        return space

    # space is None again
    if not (obs is None and axes is None):
        # check if limits are allowed
        space = Space(obs=obs, axes=axes, limits=limits)  # create and test if valid
        if one_dim_limits_only and space.n_obs > 1 and space.limits:
            raise LimitsUnderdefinedError(
                "Limits more sophisticated than 1-dim cannot be auto-created from tuples. Use `Space` instead.")
        if simple_limits_only and space.limits and space.n_limits > 1:
            raise LimitsUnderdefinedError("Limits with multiple limits cannot be auto-created"
                                          " from tuples. Use `Space` instead.")
    return space


def _reorder_indices(old: Union[List, Tuple], new: Union[List, Tuple]) -> Tuple[int]:
    new_indices = tuple(old.index(o) for o in new)
    return new_indices


def no_norm_range(func):
    """Decorator: Catch the 'norm_range' kwargs. If not None, raise NormRangeNotImplementedError."""
    parameters = inspect.signature(func).parameters
    keys = list(parameters.keys())
    if 'norm_range' in keys:
        norm_range_index = keys.index('norm_range')
    else:
        norm_range_index = None

    @functools.wraps(func)
    def new_func(*args, **kwargs):
        norm_range = kwargs.get('norm_range')
        if isinstance(norm_range, ZfitSpace):
            norm_range_not_false = not (norm_range.limits is None or norm_range.limits is False)
        else:
            norm_range_not_false = not (norm_range is None or norm_range is False)
        if norm_range_index is not None:
            norm_range_is_arg = len(args) > norm_range_index
        else:
            norm_range_is_arg = False
            kwargs.pop('norm_range', None)  # remove if in signature (= norm_range_index not None)
        if norm_range_not_false or norm_range_is_arg:
            raise NormRangeNotImplementedError()
        else:
            return func(*args, **kwargs)

    return new_func


def no_multiple_limits(func):
    """Decorator: Catch the 'limits' kwargs. If it contains multiple limits, raise MultipleLimitsNotImplementedError."""
    parameters = inspect.signature(func).parameters
    keys = list(parameters.keys())
    if 'limits' in keys:
        limits_index = keys.index('limits')
    else:
        return func  # no limits as parameters -> no problem

    @functools.wraps(func)
    def new_func(*args, **kwargs):
        limits_is_arg = len(args) > limits_index
        if limits_is_arg:
            limits = args[limits_index]
        else:
            limits = kwargs['limits']

        if limits.n_limits > 1:
            raise MultipleLimitsNotImplementedError
        else:
            return func(*args, **kwargs)

    return new_func


def supports(*, norm_range: bool = False, multiple_limits: bool = False) -> Callable:
    """Decorator: Add (mandatory for some methods) on a method to control what it can handle.

    If any of the flags is set to False, it will check the arguments and, in case they match a flag
    (say if a *norm_range* is passed while the *norm_range* flag is set to `False`), it will
    raise a corresponding exception (in this example a `NormRangeNotImplementedError`) that will
    be catched by an earlier function that knows how to handle things.

    Args:
        norm_range (bool): If False, no norm_range argument will be passed through resp. will be `None`
        multiple_limits (bool): If False, only simple limits are to be expected and no iteration is
            therefore required.
    """
    decorator_stack = []
    if not multiple_limits:
        decorator_stack.append(no_multiple_limits)
    if not norm_range:
        decorator_stack.append(no_norm_range)

    def create_deco_stack(func):
        for decorator in reversed(decorator_stack):
            func = decorator(func)
        func.__wrapped__ = supports
        return func

    return create_deco_stack


def convert_to_axes(axes, container=tuple):
    """Convert `obs` to the list of obs, also if it is a :py:class:`~ZfitSpace`. Return None if axes is None.

    """
    if axes is None:
        return axes
    axes = convert_to_container(value=axes, container=container)
    new_axes = []
    for axis in axes:
        if isinstance(axis, ZfitSpace):
            if len(axis) > 1:
                raise WorkInProgressError("Not implemented, uniqueify?")
            new_axes.extend(axis.obs)
        else:
            new_axes.append(axis)
    return container(new_axes)


def convert_to_obs_str(obs, container=tuple):
    """Convert `obs` to the list of obs, also if it is a :py:class:`~ZfitSpace`. Return None if obs is None.

    """
    if obs is None:
        return obs
    obs = convert_to_container(value=obs, container=container)
    new_obs = []
    for ob in obs:
        if isinstance(ob, ZfitSpace):
            if len(ob) > 1:
                raise WorkInProgressError("Not implemented, uniqueify?")
            new_obs.extend(ob.obs)
        else:
            new_obs.append(ob)
    return container(new_obs)


def contains_tensor(object):
    tensor_found = isinstance(object, (tf.Tensor, tf.Variable))
    with suppress(TypeError):

        for obj in object:
            if tensor_found:
                break
            tensor_found += contains_tensor(obj)
    return tensor_found


def shape_np_tf(object):
    if contains_tensor(object):
        shape = tuple(tf.convert_to_tensor(object).shape.as_list())
    else:
        shape = np.shape(object)
    return shape


def limits_consistent(spaces: Iterable["zfit.Space"]):
    """Check if space limits are the *exact* same in each obs they are defined and therefore are compatible.

    In this case, if a space has several limits, e.g. from -1 to 1 and from 2 to 3 (all in the same observable),
    to be consistent with this limits, other limits have to have (in this obs) also the limits
    from -1 to 1 and from 2 to 3. Only having the limit -1 to 1 _or_ 2 to 3 is considered _not_ consistent.

    This function is useful to check if several spaces with *different* observables can be _combined_.

    Args:
        spaces (List[zfit.Space]):

    Returns:
        bool:
    """
    try:
        new_space = combine_spaces(spaces=spaces)
    except LimitsIncompatibleError:
        return False
    return bool(new_space)


def add_spaces(spaces: Iterable["zfit.Space"]):
    """Add two spaces and merge their limits if possible or return False.

    Args:
        spaces (Iterable[:py:class:`~zfit.Space`]):

    Returns:
        Union[None, :py:class:`~zfit.Space`, bool]:

    Raises:
        LimitsIncompatibleError: if limits of the `spaces` cannot be merged because they overlap
    """
    spaces = convert_to_container(spaces)
    if not all(isinstance(space, ZfitSpace) for space in spaces):
        raise TypeError("Cannot only add type ZfitSpace")
    if len(spaces) <= 1:
        raise ValueError("Need at least two spaces to be added.")  # TODO: allow? usecase?
    obs = frozenset(frozenset(space.obs) for space in spaces)

    if len(obs) != 1:
        return False

    obs1 = spaces[0].obs
    spaces = [space.with_obs(obs=obs1) if not space.obs == obs1 else space for space in spaces]

    if limits_overlap(spaces=spaces, allow_exact_match=True):
        raise LimitsIncompatibleError("Limits of spaces overlap, cannot merge spaces.")

    lowers = []
    uppers = []
    for space in spaces:
        if space.limits is None:
            continue
        for lower, upper in space:
            for other_lower, other_upper in zip(lowers, uppers):
                lower_same = np.allclose(lower, other_lower)
                upper_same = np.allclose(upper, other_upper)
                assert not lower_same ^ upper_same, "Bug, please report as issue. limits_overlap did not catch right."
                if lower_same and upper_same:
                    break
            else:
                lowers.append(lower)
                uppers.append(upper)
    lowers = tuple(lowers)
    uppers = tuple(uppers)
    if len(lowers) == 0:
        limits = None
    else:
        limits = lowers, uppers
    new_space = zfit.Space(obs=spaces[0].obs, limits=limits)
    return new_space


combine_spaces = combine_spaces_new
