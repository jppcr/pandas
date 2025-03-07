from __future__ import annotations

from collections.abc import Callable  # noqa: PDF001
import re
from typing import (
    Union,
    overload,
)

import numpy as np

from pandas._libs import (
    lib,
    missing as libmissing,
)
from pandas._typing import (
    Dtype,
    NpDtype,
    PositionalIndexer,
    Scalar,
    ScalarIndexer,
    SequenceIndexer,
    npt,
)
from pandas.compat import (
    pa_version_under1p01,
    pa_version_under2p0,
    pa_version_under3p0,
    pa_version_under4p0,
)

from pandas.core.dtypes.common import (
    is_bool_dtype,
    is_dtype_equal,
    is_integer,
    is_integer_dtype,
    is_object_dtype,
    is_scalar,
    is_string_dtype,
    pandas_dtype,
)
from pandas.core.dtypes.missing import isna

from pandas.core.arraylike import OpsMixin
from pandas.core.arrays.arrow import ArrowExtensionArray
from pandas.core.arrays.boolean import BooleanDtype
from pandas.core.arrays.integer import Int64Dtype
from pandas.core.arrays.numeric import NumericDtype
from pandas.core.arrays.string_ import (
    BaseStringArray,
    StringDtype,
)
from pandas.core.indexers import (
    check_array_indexer,
    unpack_tuple_and_ellipses,
)
from pandas.core.strings.object_array import ObjectStringArrayMixin

if not pa_version_under1p01:
    import pyarrow as pa
    import pyarrow.compute as pc

    ARROW_CMP_FUNCS = {
        "eq": pc.equal,
        "ne": pc.not_equal,
        "lt": pc.less,
        "gt": pc.greater,
        "le": pc.less_equal,
        "ge": pc.greater_equal,
    }

ArrowStringScalarOrNAT = Union[str, libmissing.NAType]


def _chk_pyarrow_available() -> None:
    if pa_version_under1p01:
        msg = "pyarrow>=1.0.0 is required for PyArrow backed StringArray."
        raise ImportError(msg)


# TODO: Inherit directly from BaseStringArrayMethods. Currently we inherit from
# ObjectStringArrayMixin because we want to have the object-dtype based methods as
# fallback for the ones that pyarrow doesn't yet support


class ArrowStringArray(
    OpsMixin, ArrowExtensionArray, BaseStringArray, ObjectStringArrayMixin
):
    """
    Extension array for string data in a ``pyarrow.ChunkedArray``.

    .. versionadded:: 1.2.0

    .. warning::

       ArrowStringArray is considered experimental. The implementation and
       parts of the API may change without warning.

    Parameters
    ----------
    values : pyarrow.Array or pyarrow.ChunkedArray
        The array of data.

    Attributes
    ----------
    None

    Methods
    -------
    None

    See Also
    --------
    array
        The recommended function for creating a ArrowStringArray.
    Series.str
        The string methods are available on Series backed by
        a ArrowStringArray.

    Notes
    -----
    ArrowStringArray returns a BooleanArray for comparison methods.

    Examples
    --------
    >>> pd.array(['This is', 'some text', None, 'data.'], dtype="string[pyarrow]")
    <ArrowStringArray>
    ['This is', 'some text', <NA>, 'data.']
    Length: 4, dtype: string
    """

    def __init__(self, values) -> None:
        self._dtype = StringDtype(storage="pyarrow")
        if isinstance(values, pa.Array):
            self._data = pa.chunked_array([values])
        elif isinstance(values, pa.ChunkedArray):
            self._data = values
        else:
            raise ValueError(f"Unsupported type '{type(values)}' for ArrowStringArray")

        if not pa.types.is_string(self._data.type):
            raise ValueError(
                "ArrowStringArray requires a PyArrow (chunked) array of string type"
            )

    @classmethod
    def _from_sequence(cls, scalars, dtype: Dtype | None = None, copy: bool = False):
        from pandas.core.arrays.masked import BaseMaskedArray

        _chk_pyarrow_available()

        if dtype and not (isinstance(dtype, str) and dtype == "string"):
            dtype = pandas_dtype(dtype)
            assert isinstance(dtype, StringDtype) and dtype.storage == "pyarrow"

        if isinstance(scalars, BaseMaskedArray):
            # avoid costly conversion to object dtype in ensure_string_array and
            # numerical issues with Float32Dtype
            na_values = scalars._mask
            result = scalars._data
            result = lib.ensure_string_array(result, copy=copy, convert_na_value=False)
            return cls(pa.array(result, mask=na_values, type=pa.string()))

        # convert non-na-likes to str
        result = lib.ensure_string_array(scalars, copy=copy)
        return cls(pa.array(result, type=pa.string(), from_pandas=True))

    @classmethod
    def _from_sequence_of_strings(
        cls, strings, dtype: Dtype | None = None, copy: bool = False
    ):
        return cls._from_sequence(strings, dtype=dtype, copy=copy)

    @property
    def dtype(self) -> StringDtype:
        """
        An instance of 'string[pyarrow]'.
        """
        return self._dtype

    def __array__(self, dtype: NpDtype | None = None) -> np.ndarray:
        """Correctly construct numpy arrays when passed to `np.asarray()`."""
        return self.to_numpy(dtype=dtype)

    def to_numpy(
        self,
        dtype: npt.DTypeLike | None = None,
        copy: bool = False,
        na_value=lib.no_default,
    ) -> np.ndarray:
        """
        Convert to a NumPy ndarray.
        """
        # TODO: copy argument is ignored

        result = np.array(self._data, dtype=dtype)
        if self._data.null_count > 0:
            if na_value is lib.no_default:
                if dtype and np.issubdtype(dtype, np.floating):
                    return result
                na_value = self._dtype.na_value
            mask = self.isna()
            result[mask] = na_value
        return result

    @overload
    def __getitem__(self, item: ScalarIndexer) -> ArrowStringScalarOrNAT:
        ...

    @overload
    def __getitem__(self: ArrowStringArray, item: SequenceIndexer) -> ArrowStringArray:
        ...

    def __getitem__(
        self: ArrowStringArray, item: PositionalIndexer
    ) -> ArrowStringArray | ArrowStringScalarOrNAT:
        """Select a subset of self.

        Parameters
        ----------
        item : int, slice, or ndarray
            * int: The position in 'self' to get.
            * slice: A slice object, where 'start', 'stop', and 'step' are
              integers or None
            * ndarray: A 1-d boolean NumPy ndarray the same length as 'self'

        Returns
        -------
        item : scalar or ExtensionArray

        Notes
        -----
        For scalar ``item``, return a scalar value suitable for the array's
        type. This should be an instance of ``self.dtype.type``.
        For slice ``key``, return an instance of ``ExtensionArray``, even
        if the slice is length 0 or 1.
        For a boolean mask, return an instance of ``ExtensionArray``, filtered
        to the values where ``item`` is True.
        """
        item = check_array_indexer(self, item)

        if isinstance(item, np.ndarray):
            if not len(item):
                return type(self)(pa.chunked_array([], type=pa.string()))
            elif is_integer_dtype(item.dtype):
                return self.take(item)
            elif is_bool_dtype(item.dtype):
                return type(self)(self._data.filter(item))
            else:
                raise IndexError(
                    "Only integers, slices and integer or "
                    "boolean arrays are valid indices."
                )
        elif isinstance(item, tuple):
            item = unpack_tuple_and_ellipses(item)

        # error: Non-overlapping identity check (left operand type:
        # "Union[Union[int, integer[Any]], Union[slice, List[int],
        # ndarray[Any, Any]]]", right operand type: "ellipsis")
        if item is Ellipsis:  # type: ignore[comparison-overlap]
            # TODO: should be handled by pyarrow?
            item = slice(None)

        if is_scalar(item) and not is_integer(item):
            # e.g. "foo" or 2.5
            # exception message copied from numpy
            raise IndexError(
                r"only integers, slices (`:`), ellipsis (`...`), numpy.newaxis "
                r"(`None`) and integer or boolean arrays are valid indices"
            )
        # We are not an array indexer, so maybe e.g. a slice or integer
        # indexer. We dispatch to pyarrow.
        value = self._data[item]
        if isinstance(value, pa.ChunkedArray):
            return type(self)(value)
        else:
            return self._as_pandas_scalar(value)

    def _as_pandas_scalar(self, arrow_scalar: pa.Scalar):
        scalar = arrow_scalar.as_py()
        if scalar is None:
            return self._dtype.na_value
        else:
            return scalar

    def _cmp_method(self, other, op):
        from pandas.arrays import BooleanArray

        pc_func = ARROW_CMP_FUNCS[op.__name__]
        if isinstance(other, ArrowStringArray):
            result = pc_func(self._data, other._data)
        elif isinstance(other, (np.ndarray, list)):
            result = pc_func(self._data, other)
        elif is_scalar(other):
            try:
                result = pc_func(self._data, pa.scalar(other))
            except (pa.lib.ArrowNotImplementedError, pa.lib.ArrowInvalid):
                mask = isna(self) | isna(other)
                valid = ~mask
                result = np.zeros(len(self), dtype="bool")
                result[valid] = op(np.array(self)[valid], other)
                return BooleanArray(result, mask)
        else:
            return NotImplemented

        if pa_version_under2p0:
            result = result.to_pandas().values
        else:
            result = result.to_numpy()
        return BooleanArray._from_sequence(result)

    def insert(self, loc: int, item):
        if not isinstance(item, str) and item is not libmissing.NA:
            raise TypeError("Scalar must be NA or str")
        return super().insert(loc, item)

    def _maybe_convert_setitem_value(self, value):
        """Maybe convert value to be pyarrow compatible."""
        if is_scalar(value):
            if isna(value):
                value = None
            elif not isinstance(value, str):
                raise ValueError("Scalar must be NA or str")
        else:
            value = np.array(value, dtype=object, copy=True)
            value[isna(value)] = None
            for v in value:
                if not (v is None or isinstance(v, str)):
                    raise ValueError("Scalar must be NA or str")
        return value

    def isin(self, values):
        if pa_version_under2p0:
            return super().isin(values)

        value_set = [
            pa_scalar.as_py()
            for pa_scalar in [pa.scalar(value, from_pandas=True) for value in values]
            if pa_scalar.type in (pa.string(), pa.null())
        ]

        # for an empty value_set pyarrow 3.0.0 segfaults and pyarrow 2.0.0 returns True
        # for null values, so we short-circuit to return all False array.
        if not len(value_set):
            return np.zeros(len(self), dtype=bool)

        kwargs = {}
        if pa_version_under3p0:
            # in pyarrow 2.0.0 skip_null is ignored but is a required keyword and raises
            # with unexpected keyword argument in pyarrow 3.0.0+
            kwargs["skip_null"] = True

        result = pc.is_in(self._data, value_set=pa.array(value_set), **kwargs)
        # pyarrow 2.0.0 returned nulls, so we explicily specify dtype to convert nulls
        # to False
        return np.array(result, dtype=np.bool_)

    def astype(self, dtype, copy: bool = True):
        dtype = pandas_dtype(dtype)

        if is_dtype_equal(dtype, self.dtype):
            if copy:
                return self.copy()
            return self

        elif isinstance(dtype, NumericDtype):
            data = self._data.cast(pa.from_numpy_dtype(dtype.numpy_dtype))
            return dtype.__from_arrow__(data)

        return super().astype(dtype, copy=copy)

    # ------------------------------------------------------------------------
    # String methods interface

    # error: Cannot determine type of 'na_value'
    _str_na_value = StringDtype.na_value  # type: ignore[has-type]

    def _str_map(
        self, f, na_value=None, dtype: Dtype | None = None, convert: bool = True
    ):
        # TODO: de-duplicate with StringArray method. This method is moreless copy and
        # paste.

        from pandas.arrays import (
            BooleanArray,
            IntegerArray,
        )

        if dtype is None:
            dtype = self.dtype
        if na_value is None:
            na_value = self.dtype.na_value

        mask = isna(self)
        arr = np.asarray(self)

        if is_integer_dtype(dtype) or is_bool_dtype(dtype):
            constructor: type[IntegerArray] | type[BooleanArray]
            if is_integer_dtype(dtype):
                constructor = IntegerArray
            else:
                constructor = BooleanArray

            na_value_is_na = isna(na_value)
            if na_value_is_na:
                na_value = 1
            result = lib.map_infer_mask(
                arr,
                f,
                mask.view("uint8"),
                convert=False,
                na_value=na_value,
                # error: Argument 1 to "dtype" has incompatible type
                # "Union[ExtensionDtype, str, dtype[Any], Type[object]]"; expected
                # "Type[object]"
                dtype=np.dtype(dtype),  # type: ignore[arg-type]
            )

            if not na_value_is_na:
                mask[:] = False

            return constructor(result, mask)

        elif is_string_dtype(dtype) and not is_object_dtype(dtype):
            # i.e. StringDtype
            result = lib.map_infer_mask(
                arr, f, mask.view("uint8"), convert=False, na_value=na_value
            )
            result = pa.array(result, mask=mask, type=pa.string(), from_pandas=True)
            return type(self)(result)
        else:
            # This is when the result type is object. We reach this when
            # -> We know the result type is truly object (e.g. .encode returns bytes
            #    or .findall returns a list).
            # -> We don't know the result type. E.g. `.get` can return anything.
            return lib.map_infer_mask(arr, f, mask.view("uint8"))

    def _str_contains(self, pat, case=True, flags=0, na=np.nan, regex: bool = True):
        if flags:
            return super()._str_contains(pat, case, flags, na, regex)

        if regex:
            if pa_version_under4p0 or case is False:
                return super()._str_contains(pat, case, flags, na, regex)
            else:
                result = pc.match_substring_regex(self._data, pat)
        else:
            if case:
                result = pc.match_substring(self._data, pat)
            else:
                result = pc.match_substring(pc.utf8_upper(self._data), pat.upper())
        result = BooleanDtype().__from_arrow__(result)
        if not isna(na):
            result[isna(result)] = bool(na)
        return result

    def _str_startswith(self, pat: str, na=None):
        if pa_version_under4p0:
            return super()._str_startswith(pat, na)

        pat = "^" + re.escape(pat)
        return self._str_contains(pat, na=na, regex=True)

    def _str_endswith(self, pat: str, na=None):
        if pa_version_under4p0:
            return super()._str_endswith(pat, na)

        pat = re.escape(pat) + "$"
        return self._str_contains(pat, na=na, regex=True)

    def _str_replace(
        self,
        pat: str | re.Pattern,
        repl: str | Callable,
        n: int = -1,
        case: bool = True,
        flags: int = 0,
        regex: bool = True,
    ):
        if (
            pa_version_under4p0
            or isinstance(pat, re.Pattern)
            or callable(repl)
            or not case
            or flags
        ):
            return super()._str_replace(pat, repl, n, case, flags, regex)

        func = pc.replace_substring_regex if regex else pc.replace_substring
        result = func(self._data, pattern=pat, replacement=repl, max_replacements=n)
        return type(self)(result)

    def _str_match(
        self, pat: str, case: bool = True, flags: int = 0, na: Scalar | None = None
    ):
        if pa_version_under4p0:
            return super()._str_match(pat, case, flags, na)

        if not pat.startswith("^"):
            pat = "^" + pat
        return self._str_contains(pat, case, flags, na, regex=True)

    def _str_fullmatch(
        self, pat, case: bool = True, flags: int = 0, na: Scalar | None = None
    ):
        if pa_version_under4p0:
            return super()._str_fullmatch(pat, case, flags, na)

        if not pat.endswith("$") or pat.endswith("//$"):
            pat = pat + "$"
        return self._str_match(pat, case, flags, na)

    def _str_isalnum(self):
        result = pc.utf8_is_alnum(self._data)
        return BooleanDtype().__from_arrow__(result)

    def _str_isalpha(self):
        result = pc.utf8_is_alpha(self._data)
        return BooleanDtype().__from_arrow__(result)

    def _str_isdecimal(self):
        result = pc.utf8_is_decimal(self._data)
        return BooleanDtype().__from_arrow__(result)

    def _str_isdigit(self):
        result = pc.utf8_is_digit(self._data)
        return BooleanDtype().__from_arrow__(result)

    def _str_islower(self):
        result = pc.utf8_is_lower(self._data)
        return BooleanDtype().__from_arrow__(result)

    def _str_isnumeric(self):
        result = pc.utf8_is_numeric(self._data)
        return BooleanDtype().__from_arrow__(result)

    def _str_isspace(self):
        if pa_version_under2p0:
            return super()._str_isspace()

        result = pc.utf8_is_space(self._data)
        return BooleanDtype().__from_arrow__(result)

    def _str_istitle(self):
        result = pc.utf8_is_title(self._data)
        return BooleanDtype().__from_arrow__(result)

    def _str_isupper(self):
        result = pc.utf8_is_upper(self._data)
        return BooleanDtype().__from_arrow__(result)

    def _str_len(self):
        if pa_version_under4p0:
            return super()._str_len()

        result = pc.utf8_length(self._data)
        return Int64Dtype().__from_arrow__(result)

    def _str_lower(self):
        return type(self)(pc.utf8_lower(self._data))

    def _str_upper(self):
        return type(self)(pc.utf8_upper(self._data))

    def _str_strip(self, to_strip=None):
        if pa_version_under4p0:
            return super()._str_strip(to_strip)

        if to_strip is None:
            result = pc.utf8_trim_whitespace(self._data)
        else:
            result = pc.utf8_trim(self._data, characters=to_strip)
        return type(self)(result)

    def _str_lstrip(self, to_strip=None):
        if pa_version_under4p0:
            return super()._str_lstrip(to_strip)

        if to_strip is None:
            result = pc.utf8_ltrim_whitespace(self._data)
        else:
            result = pc.utf8_ltrim(self._data, characters=to_strip)
        return type(self)(result)

    def _str_rstrip(self, to_strip=None):
        if pa_version_under4p0:
            return super()._str_rstrip(to_strip)

        if to_strip is None:
            result = pc.utf8_rtrim_whitespace(self._data)
        else:
            result = pc.utf8_rtrim(self._data, characters=to_strip)
        return type(self)(result)
