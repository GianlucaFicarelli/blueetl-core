"""Core utilities."""

import operator
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from itertools import chain
from operator import attrgetter
from typing import Any, Optional, Union

import numpy as np
import pandas as pd
from pandas.api.types import is_list_like

from blueetl_core.logging import L

COMPARISON_OPERATORS = {
    "eq": attrgetter("__eq__"),
    "ne": attrgetter("__ne__"),
    "le": attrgetter("__le__"),
    "lt": attrgetter("__lt__"),
    "ge": attrgetter("__ge__"),
    "gt": attrgetter("__gt__"),
    "isin": attrgetter("isin"),
    "regex": attrgetter("str.contains"),
}


def ensure_list(x: Any) -> list:
    """Always return a list from the given argument."""
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    return [x]


def _and_or_mask(
    query_list: list[dict[str, Any]],
    filter_func: Callable[[dict[str, Any]], list[np.ndarray]],
) -> Optional[np.ndarray]:
    """Return the and/or mask obtained after processing each dictionary in query_list.

    Args:
        query_list: list of query dictionaries, that should be processed and OR-ed together.
        filter_func: callable accepting a query dict and returning a list of masks,
            that will be AND-ed together.

    Returns:
        The resulting mask, or None if no queries have been provided.
    """
    or_masks = []
    for query in query_list:
        if query:
            and_masks = filter_func(query)
            # minor optimization: check if len == 1
            or_masks.append(and_masks[0] if len(and_masks) == 1 else np.all(and_masks, axis=0))
    if not or_masks:
        # no filters
        return None
    # minor optimization: check if len == 1
    return or_masks[0] if len(or_masks) == 1 else np.any(or_masks, axis=0)


def query_frame(df: pd.DataFrame, query_list: list[dict[str, Any]]) -> pd.DataFrame:
    """Given a query dictionary, return the DataFrame filtered by columns and index."""

    def _filter_func(query: dict[str, Any]) -> list[np.ndarray]:
        # dictionary with query keys split into columns and index
        q: dict[str, Any] = {"columns": {}, "index": {}}
        for key, value in query.items():
            q[mapping[key]][key] = value
        # filter by columns and index
        return list(
            chain(
                (compare(df[key], val) for key, val in q["columns"].items()),
                (compare(df.index.get_level_values(key), val) for key, val in q["index"].items()),
            )
        )

    # map each name to columns or index;
    # if the same key is present in both columns and index, use columns
    mapping = {
        **{k: "index" for k in df.index.names if k is not None},
        **{k: "columns" for k in df.columns if k is not None},
    }
    mask = _and_or_mask(query_list, _filter_func)
    return df.loc[mask] if mask is not None else df


def query_series(series: pd.Series, query_list: list[dict[str, Any]]) -> pd.Series:
    """Given a query dictionary, return the Series filtered by index."""

    def _filter_func(query: dict[str, Any]) -> list[np.ndarray]:
        # filter by index
        return list(compare(series.index.get_level_values(key), val) for key, val in query.items())

    mask = _and_or_mask(query_list, _filter_func)
    return series.loc[mask] if mask is not None else series


def compare(obj: Union[pd.Series, pd.Index], value: Any) -> np.ndarray:
    """Return the result of the comparison between obj and value.

    Args:
        obj: Series, or Index.
        value: value used for comparison.
            - if scalar, use equality
            - if list-like, use isin
            - if dict, any supported operators can be specified, and they will be AND-ed together

    Examples:
        >>> df = pd.DataFrame({"gid": [0, 2, 3, 7, 8]})
        >>> compare(df["gid"], 3)
            array([False, False,  True, False, False])
        >>> compare(df["gid"], [3, 5, 8])
            array([False, False,  True, False,  True])
        >>> compare(df["gid"], {"ge": 3, "lt": 8})
            array([False, False,  True,  True, False])

    """
    if isinstance(value, dict):
        if not value:
            raise ValueError("Empty filter")
        if unsupported := [op for op in value if op not in COMPARISON_OPERATORS]:
            raise ValueError(f"Unsupported operator(s): {unsupported}")
        masks = [COMPARISON_OPERATORS[op](obj)(v) for op, v in value.items()]
        return masks[0] if len(masks) == 1 else np.all(masks, axis=0)
    if is_list_like(value):
        return np.asarray(obj.isin(value))
    # more efficient than using isin with a list of one element
    return np.asarray(obj == value)


def is_subfilter(left: dict, right: dict, strict: bool = False) -> bool:
    """Return True if ``left`` is a subfilter of ``right``, False otherwise.

    Args:
        left: left filter dict.
        right: right filter dict.
        strict: if False, ``left`` is a subfilter of ``right`` if it's equal or more specific;
            if True, ``left`` is a subfilter of ``right`` only if it's more specific.

    Examples:
        >>> print(is_subfilter({}, {}))
        True
        >>> print(is_subfilter({}, {}, strict=True))
        False
        >>> print(is_subfilter({}, {"key": 1}))
        False
        >>> print(is_subfilter({"key": 1}, {}))
        True
        >>> print(is_subfilter({"key": 1}, {"key": 1}))
        True
        >>> print(is_subfilter({"key": 1}, {"key": 1}, strict=True))
        False
        >>> print(is_subfilter({"key": 1}, {"key": [1]}))
        True
        >>> print(is_subfilter({"key": 1}, {"key": [1]}, strict=True))
        False
        >>> print(is_subfilter({"key": 1}, {"key": [1, 2]}))
        True
        >>> print(is_subfilter({"key": 1}, {"key": {"isin": [1, 2]}}))
        True
        >>> print(is_subfilter({"key": 1}, {"key": 2}))
        False
        >>> print(is_subfilter({"key": 1}, {"key": [2, 3]}))
        False
        >>> print(is_subfilter({"key": 1}, {"key": {"isin": [2, 3]}}))
        False
        >>> print(is_subfilter({"key1": 1, "key2": 2}, {"key1": 1}))
        True
        >>> print(is_subfilter({"key1": 1}, {"key1": 1, "key2": 2}))
        False
    """

    def _to_dict(obj) -> dict:
        """Return a normalized filter, i.e. a dict where "eq" is replaced by "isin"."""
        obj = deepcopy(obj)
        if isinstance(obj, dict):
            if "eq" in obj:
                # convert "eq" to "isin", and set "isin" to the new value,
                # or to an empty list if "eq" and "isin" are incompatible
                value = obj.pop("eq")
                obj["isin"] = [value] if "isin" not in obj or value in obj["isin"] else []
            return obj
        if isinstance(obj, list):
            return {"isin": obj}
        # any other type of object is considered for equality with "isin"
        return {"isin": [obj]}

    def _is_subdict(d1: dict, d2: dict) -> bool:
        """Return True if d1 is a subdict of d2, or d1 and d2 are equal."""
        # mapping operator -> operation
        operators = {
            "ne": operator.eq,
            "le": operator.le,
            "lt": operator.le,
            "ge": operator.ge,
            "gt": operator.ge,
            "isin": lambda a, b: set(a).issubset(b),
        }
        assert set(operators).issuperset(d1), "Invalid keys in d1"
        assert set(operators).issuperset(d2), "Invalid keys in d2"
        unmatched_keys = set()
        # for each operator in the operators mapping,
        # if the operator is present in d2 but not in d1,
        # or if the given operation is not satisfied,
        # then d1 cannot be a subdict of d2
        for op, operation in operators.items():
            if op in d2 and (op not in d1 or not operation(d1[op], d2[op])):
                unmatched_keys.add(op)
        L.debug("unmatched keys: %s", sorted(unmatched_keys))
        return len(unmatched_keys) == 0

    # keys present in left, but missing or different in right
    difference = set(left)
    for key in right:
        if key not in left:
            return False
        dict_left = _to_dict(left[key])
        dict_right = _to_dict(right[key])
        if strict and dict_left == dict_right:
            difference.remove(key)
            continue
        if not _is_subdict(dict_left, dict_right):
            return False
    return not strict or len(difference) > 0


def smart_concat(iterable, *, keys=None, copy=False, skip_empty=True, **kwargs):
    """Build and return a Series or a Dataframe from an iterable of objects with the same index.

    This is similar to ``pd.concat``, but the result is consistent even when the levels of the
    indexes are ordered differently, while ``pd.concat`` would blindly concatenate the indexes,
    ignoring and removing the names of the levels.

    Moreover, it uses ``copy=False`` by default, that's safe only if the original data isn't going
    to change, but it's more efficient, especially when concatenating a single item.

    Args:
        iterable: iterable or mapping of Series or DataFrames.
            All the objects must be of the same type, and they must have the same index,
            or an exception is raised.
        keys: passed to pd.concat. If multiple levels passed, should contain tuples. Construct
            hierarchical index using the passed keys as the outermost level.
        copy: passed to pd.concat. If the original data can be used without making a copy, then
            it can be set to False.
        skip_empty: if True, empty objects are skipped, unless they are all empty. If False, they
            are all passed to pd.concat, and the result may depend on the Pandas version.
            Note that in the latter case, you may see a FutureWarning with Pandas 2:

                FutureWarning: The behavior of DataFrame concatenation with empty or all-NA entries
                is deprecated. In a future version, this will no longer exclude empty or all-NA
                columns when determining the result dtypes. To retain the old behavior, exclude the
                relevant entries before the concat operation.
        kwargs: other keyword arguments to be passed to pd.concat

    Returns:
        (pd.Series|pd.DataFrame) result of the concatenation, same type of the input elements.

    Examples:
        >>> idx1 = pd.MultiIndex.from_tuples([(10, 11), (20, 21)], names=["i1", "i2"])
        >>> idx2 = pd.MultiIndex.from_tuples([(11, 10), (31, 30)], names=["i2", "i1"])
        >>> df1 = pd.DataFrame({"A": [1, 2], "B": [3, 4]}, index=idx1)
        >>> df2 = pd.DataFrame({"A": [5, 6], "B": [7, 8]}, index=idx2)
        >>> pd.concat([df1, df2])  # index levels are lost
               A  B
        10 11  1  3
        20 21  2  4
        11 10  5  7
        31 30  6  8
        >>> smart_concat([df1, df2])  # index levels are preserved
               A  B
        i1 i2
        10 11  1  3
        20 21  2  4
        10 11  5  7
        30 31  6  8
        >>> pd.concat([df1, df2], axis=1)  # index levels are lost
                 A    B    A    B
        10 11  1.0  3.0  NaN  NaN
        20 21  2.0  4.0  NaN  NaN
        11 10  NaN  NaN  5.0  7.0
        31 30  NaN  NaN  6.0  8.0
        >>> smart_concat([df1, df2], axis=1)  # index levels are preserved
                 A    B    A    B
        i1 i2
        10 11  1.0  3.0  5.0  7.0
        20 21  2.0  4.0  NaN  NaN
        30 31  NaN  NaN  6.0  8.0
    """

    def _reorder_levels(obj, order):
        # wrap reorder_levels to raise an explicit error when needed
        if len(order) != obj.index.nlevels:
            # reorder_levels would raise an AssertionError
            raise RuntimeError(
                f"Length of order must be same as number of "
                f"levels ({obj.index.nlevels}), got {len(order)}"
            )
        if diff := set(order).difference(obj.index.names):
            # reorder_levels would raise a KeyError
            raise RuntimeError(f"Levels not found: {''.join(diff)}")
        return obj.reorder_levels(order)

    def _ordered(obj):
        nonlocal order
        if order is None:
            order = obj.index.names
        return obj if order == obj.index.names else _reorder_levels(obj, order)

    order = None
    if isinstance(iterable, Mapping):
        mapping = iterable
        keys = keys if keys is not None else mapping.keys()
        iterable = (mapping[key] for key in keys)
    objects = [_ordered(obj) for obj in iterable]
    if skip_empty and not all(obj.empty for obj in objects):
        objects = [obj for obj in objects if not obj.empty]
    return pd.concat(objects, keys=keys, copy=copy, **kwargs)


def concat_tuples(iterable, *args, **kwargs):
    """Build and return a Series from an iterable of tuples (value, conditions).

    Args:
        iterable: iterable of tuples (value, conditions), where

            - value is a single value that will be added to the Series
            - conditions is a dict containing the conditions to be used for the MultiIndex.
              The keys of the conditions must be the same for each tuple of the iterable,
              or an exception is raised.

        args: positional arguments to be passed to pd.concat
        kwargs: key arguments to be passed to pd.concat

    Returns:
        (pd.Series) result of the concatenation.
    """

    def _index(conditions):
        arrays = [[v] for v in conditions.values()]
        names = list(conditions)
        return pd.MultiIndex.from_arrays(arrays, names=names)

    iterable = (pd.Series([data], index=_index(conditions)) for data, conditions in iterable)
    return smart_concat(iterable, *args, **kwargs)


def longest_match_count(iter1, iter2) -> int:
    """Return the number of matching elements from the beginning of the given iterables."""
    count = 0
    for i1, i2 in zip(iter1, iter2):
        if i1 != i2:
            break
        count += 1
    return count


@dataclass
class CachedItem:
    """Item of CachedDataFrame."""

    df: pd.DataFrame
    key: str
    value: Any

    def __eq__(self, other: object) -> bool:
        """Return True if the objects are considered equal, False otherwise."""
        if not isinstance(other, CachedItem):
            return NotImplemented
        return self.key == other.key and self.value == other.value and self.df.equals(other.df)


class CachedDataFrame:
    """DataFrame wrapper to cache partial queries."""

    def __init__(self, df: pd.DataFrame) -> None:
        """Initialize the object with the base DataFrame.

        The internal stack will contain CachedItems, each one containing a DataFrame filtered by
        the corresponding key and value, and by all the previous keys and values in the stack.

        Examples:
            .. code-block:: python

                self._stack = [
                    CachedItem(df=df0, key="simulation_id", value=1),
                    CachedItem(df=df1, key="circuit_id", value=0),
                    CachedItem(df=df2, key="window", value="w1"),
                    CachedItem(df=df3, key="trial", value=0),
                ]

            where:

            - ``df0`` is ``self._df`` filtered by ``simulation_id=1``
            - ``df1`` is ``df0`` filtered by ``circuit_id=0``
            - ``df2`` is ``df1`` filtered by ``window="w1"``
            - ``df3`` is ``df2`` filtered by ``trial=0``

        """
        self._df = df
        self._valid_keys = {*df.columns, *(key for key in df.index.names if key)}
        self._stack: list[CachedItem] = []
        self._matched = 0  # for test and debug

    def _longest_keys_count(self, keys) -> int:
        return longest_match_count((item.key for item in self._stack), keys)

    def _longest_values_count(self, values) -> int:
        return longest_match_count((item.value for item in self._stack), values)

    def query(self, query: dict[str, Any], ignore_unknown_keys: bool = False) -> pd.DataFrame:
        """Return the DataFrame filtered by query, using cached DataFrames if possible.

        - The order of the keys in the query dict is important.
        - The cache is reused only when the keys and their order are the same.
        - The cache is reused also when only some keys and their values match.

        Args:
            query: dict to be passed to ``etl.q``.
            ignore_unknown_keys: if True, ignore keys specified in the query but not present in the
            DataFrame columns or in the index level names. If False, unknown keys raise an error.

        """
        if ignore_unknown_keys:
            query = {key: value for key, value in query.items() if key in self._valid_keys}
        query_keys = tuple(query.keys())
        query_values = tuple(query.values())
        # find the cached dataframe with the longest key
        self._matched = min(
            self._longest_keys_count(query_keys),
            self._longest_values_count(query_values),
        )
        self._stack = self._stack[: self._matched]
        df = self._stack[-1].df if self._stack else self._df
        # update the cache for every partial key, if needed
        while len(self._stack) < len(query):
            col = query_keys[len(self._stack)]
            val = query_values[len(self._stack)]
            df = df.etl.q({col: val})
            self._stack.append(CachedItem(df=df, key=col, value=val))
        return df
