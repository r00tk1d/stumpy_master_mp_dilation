# STUMPY
# Copyright 2019 TD Ameritrade. Released under the terms of the 3-Clause BSD license.
# STUMPY is a trademark of TD Ameritrade IP Company, Inc. All rights reserved.

import numpy as np
import math

from . import core, stump, stumped
from .aampdist import aampdist, aampdisted


def _compute_P_ABBA(T_A, T_B, m, P_ABBA, client=None, device_id=None, mp_func=stump):
    """
    A convenience function for computing the (unsorted) concatenated matrix profiles
    from an AB-join and BA-join for the two time series, `T_A` and `T_B`. This result
    can then be used to compute the matrix profile distance (MPdist) measure.

    The MPdist distance measure considers two time series to be similar if they share
    many subsequences, regardless of the order of matching subsequences. MPdist
    concatenates the output of an AB-join and a BA-join and returns the `k`th smallest
    value as the reported distance. Note that MPdist is a measure and not a metric.
    Therefore, it does not obey the triangular inequality but the method is highly
    scalable.

    Parameters
    ----------
    T_A : numpy.ndarray
        The first time series or sequence for which to compute the matrix profile

    T_B : numpy.ndarray
        The second time series or sequence for which to compute the matrix profile

    m : int
        Window size

    P_ABBA : numpy.ndarray
        The output array to write the concatenated AB-join and BA-join results to

    client : client, default None
        A Dask or Ray Distributed client. Setting up a distributed cluster is beyond
        the scope of this library. Please refer to the Dask or Ray Distributed
        documentation.

    device_id : int or list, default None
        The (GPU) device number to use. The default value is `0`. A list of
        valid device ids (int) may also be provided for parallel GPU-STUMP
        computation. A list of all valid device ids can be obtained by
        executing `[device.id for device in numba.cuda.list_devices()]`.

    mp_func : object, default stump
        Specify a custom matrix profile function to use for computing matrix profiles

    Returns
    -------
    None

    Notes
    -----
    `DOI: 10.1109/ICDM.2018.00119 \
    <https://www.cs.ucr.edu/~eamonn/MPdist_Expanded.pdf>`__

    See Section III
    """
    n_A = T_A.shape[0]
    partial_mp_func = core._get_partial_mp_func(
        mp_func, client=client, device_id=device_id
    )

    P_ABBA[: n_A - m + 1] = partial_mp_func(T_A, m, T_B, ignore_trivial=False)[:, 0]
    P_ABBA[n_A - m + 1 :] = partial_mp_func(T_B, m, T_A, ignore_trivial=False)[:, 0]


def _mpdist(
    T_A,
    T_B,
    m,
    percentage=0.05,
    k=None,
    client=None,
    device_id=None,
    mp_func=stump,
    custom_func=None,
):
    """
    A convenience function for computing the matrix profile distance (MPdist) measure
    between any two time series.

    The MPdist distance measure considers two time series to be similar if they share
    many subsequences, regardless of the order of matching subsequences. MPdist
    concatenates the output of an AB-join and a BA-join and returns the `k`th smallest
    value as the reported distance. Note that MPdist is a measure and not a metric.
    Therefore, it does not obey the triangular inequality but the method is highly
    scalable.

    Parameters
    ----------
    T_A : numpy.ndarray
        The first time series or sequence for which to compute the matrix profile

    T_B : numpy.ndarray
        The second time series or sequence for which to compute the matrix profile

    m : int
        Window size

    percentage : float, 0.05
       The percentage of distances that will be used to report `mpdist`. The value
        is between 0.0 and 1.0. This parameter is ignored when `k` is not `None` or when
        `k_func` is not None.

    k : int, default None
        Specify the `k`th value in the concatenated matrix profiles to return. When `k`
        is not `None`, then the `percentage` parameter is ignored. This parameter is
        ignored when `k_func` is not None.

    client : client, default None
        A Dask or Ray Distributed client. Setting up a distributed cluster is beyond
        the scope of this library. Please refer to the Dask or Ray Distributed
        documentation.

    device_id : int or list, default None
        The (GPU) device number to use. The default value is `0`. A list of
        valid device ids (int) may also be provided for parallel GPU-STUMP
        computation. A list of all valid device ids can be obtained by
        executing `[device.id for device in numba.cuda.list_devices()]`.

    mp_func : object, default stump
        Specify a custom matrix profile function to use for computing matrix profiles

    custom_func : object, default None
        A custom user defined function for selecting the desired value from the
        unsorted `P_ABBA` array. This function may need to leverage `functools.partial`
        and should take `P_ABBA` as its only input parameter and return a single
        `MPdist` value. The `percentage` and `k` parameters are ignored when
        `custom_func` is not None.

    Returns
    -------
    MPdist : float
        The matrix profile distance

    Notes
    -----
    `DOI: 10.1109/ICDM.2018.00119 \
    <https://www.cs.ucr.edu/~eamonn/MPdist_Expanded.pdf>`__

    See Section III
    """
    n_A = T_A.shape[0]
    n_B = T_B.shape[0]
    P_ABBA = np.empty(n_A - m + 1 + n_B - m + 1, dtype=np.float64)

    _compute_P_ABBA(T_A, T_B, m, P_ABBA, client, device_id, mp_func)

    if k is not None:
        k = min(int(k), P_ABBA.shape[0] - 1)
    else:
        percentage = np.clip(percentage, 0.0, 1.0)
        k = min(math.ceil(percentage * (n_A + n_B)), n_A - m + 1 + n_B - m + 1 - 1)

    MPdist = core._select_P_ABBA_value(P_ABBA, k, custom_func)

    return MPdist


def _mpdist_vect(
    Q,
    T,
    m,
    μ_Q,
    σ_Q,
    M_T,
    Σ_T,
    percentage=0.05,
    k=None,
    custom_func=None,
):
    """
    Compute the matrix profile distance measure vector between `Q` and each subsequence,
    `T[i : i + len(Q)]`, within `T`.

    Parameters
    ----------
    Q : numpy.ndarray
        Query array

    T : numpy.ndarray
        Time series or sequence

    m : int
        Window size that will be used for calculating the mpdist between Q and
        any subsequence in T (of size `len(Q)`)

    μ_Q : numpy.ndarray
        Sliding mean of `Q`

    σ_Q : numpy.ndarray
        Sliding standard deviation of `Q`

    M_T : numpy.ndarray
        Sliding mean of `T`

    Σ_T : numpy.ndarray
        Sliding standard deviation of `T`

    percentage : float, 0.05
        The percentage of distances that will be used to report `mpdist`. The value
        is between 0.0 and 1.0. This parameter is ignored when `k` is not `None` or when
        `k_func` is not None.

    k : int, default None
        Specify the `k`th value in the concatenated matrix profiles to return. When `k`
        is not `None`, then the `percentage` parameter is ignored. This parameter is
        ignored when `custom_func` is not None.

    custom_func : object, default None
        A custom user defined function for selecting the desired value from the
        unsorted `P_ABBA` array. This function may need to leverage `functools.partial`
        and should take `P_ABBA` as its only input parameter and return a single
        `MPdist` value. The `percentage` and `k` parameters are ignored when
        `custom_func` is not None.

    Returns
    -------
    MPdist_vect : numpy.ndarray
        The mpdist-based distance profile of `Q` with `T`. It is a 1D array of
        size `len(T) - len(Q) + 1`. MPdist_vect[i] is the mpdist distance between
        `Q` and subsequence `T[i : i + len(Q)]`.
    """
    j = Q.shape[0] - m + 1  # `k` is reserved for `P_ABBA` selection
    l = T.shape[0] - m + 1
    MPdist_vect = np.empty(T.shape[0] - Q.shape[0] + 1, dtype=np.float64)
    distance_matrix = np.full((j, l), np.inf, dtype=np.float64)
    P_ABBA = np.empty(2 * j, dtype=np.float64)

    if k is None:
        percentage = np.clip(percentage, 0.0, 1.0)
        k = min(math.ceil(percentage * (2 * Q.shape[0])), 2 * j - 1)

    k = min(int(k), P_ABBA.shape[0] - 1)

    core._mass_distance_matrix(Q, T, m, distance_matrix, μ_Q, σ_Q, M_T, Σ_T)

    rolling_row_min = core.rolling_nanmin(distance_matrix, j)
    col_min = np.nanmin(distance_matrix, axis=0)

    for i in range(MPdist_vect.shape[0]):
        P_ABBA[:j] = rolling_row_min[:, i]
        P_ABBA[j:] = col_min[i : i + j]
        MPdist_vect[i] = core._select_P_ABBA_value(P_ABBA, k, custom_func)

    return MPdist_vect


@core.non_normalized(aampdist)
def mpdist(T_A, T_B, m, percentage=0.05, k=None, normalize=True, p=2.0):
    """
    Compute the z-normalized matrix profile distance (MPdist) measure between any two
    time series

    The MPdist distance measure considers two time series to be similar if they share
    many subsequences, regardless of the order of matching subsequences. MPdist
    concatenates the output of an AB-join and a BA-join and returns the `k`th smallest
    value as the reported distance. Note that MPdist is a measure and not a metric.
    Therefore, it does not obey the triangular inequality but the method is highly
    scalable.

    Parameters
    ----------
    T_A : numpy.ndarray
        The first time series or sequence for which to compute the matrix profile

    T_B : numpy.ndarray
        The second time series or sequence for which to compute the matrix profile

    m : int
        Window size

    percentage : float, default 0.05
        The percentage of distances that will be used to report `mpdist`. The value
        is between 0.0 and 1.0.

    k : int
        Specify the `k`th value in the concatenated matrix profiles to return. When `k`
        is not `None`, then the `percentage` parameter is ignored.

    normalize : bool, default True
        When set to `True`, this z-normalizes subsequences prior to computing distances.
        Otherwise, this function gets re-routed to its complementary non-normalized
        equivalent set in the `@core.non_normalized` function decorator.

    p : float, default 2.0
        The p-norm to apply for computing the Minkowski distance. This parameter is
        ignored when `normalize == True`.

    Returns
    -------
    MPdist : float
        The matrix profile distance

    See Also
    --------
    mpdisted : Compute the z-normalized matrix profile distance (MPdist) measure
        between any two time series with a distributed dask cluster
    gpu_mpdist : Compute the z-normalized matrix profile distance (MPdist) measure
        between any two time series with one or more GPU devices

    Notes
    -----
    `DOI: 10.1109/ICDM.2018.00119 \
    <https://www.cs.ucr.edu/~eamonn/MPdist_Expanded.pdf>`__

    See Section III

    Examples
    --------
    >>> stumpy.mpdist(
    ...     np.array([-11.1, 23.4, 79.5, 1001.0]),
    ...     np.array([584., -11., 23., 79., 1001., 0., -19.]),
    ...     m=3)
    0.00019935236191097894
    """
    MPdist = _mpdist(T_A, T_B, m, percentage, k, mp_func=stump)

    return MPdist


@core.non_normalized(aampdisted)
def mpdisted(client, T_A, T_B, m, percentage=0.05, k=None, normalize=True, p=2.0):
    """
    Compute the z-normalized matrix profile distance (MPdist) measure between any two
    time series with a distributed dask cluster

    The MPdist distance measure considers two time series to be similar if they share
    many subsequences, regardless of the order of matching subsequences. MPdist
    concatenates the output of an AB-join and a BA-join and returns the `k`th smallest
    value as the reported distance. Note that MPdist is a measure and not a metric.
    Therefore, it does not obey the triangular inequality but the method is highly
    scalable.

    Parameters
    ----------
    client : client
        A Dask or Ray Distributed client. Setting up a distributed cluster is beyond
        the scope of this library. Please refer to the Dask or Ray Distributed
        documentation.

    T_A : numpy.ndarray
        The first time series or sequence for which to compute the matrix profile

    T_B : numpy.ndarray
        The second time series or sequence for which to compute the matrix profile

    m : int
        Window size

    percentage : float, default 0.05
        The percentage of distances that will be used to report `mpdist`. The value
        is between 0.0 and 1.0. This parameter is ignored when `k` is not `None`.

    k : int
        Specify the `k`th value in the concatenated matrix profiles to return. When `k`
        is not `None`, then the `percentage` parameter is ignored.

    normalize : bool, default True
        When set to `True`, this z-normalizes subsequences prior to computing distances.
        Otherwise, this function gets re-routed to its complementary non-normalized
        equivalent set in the `@core.non_normalized` function decorator.

    p : float, default 2.0
        The p-norm to apply for computing the Minkowski distance. This parameter is
        ignored when `normalize == True`.

    Returns
    -------
    MPdist : float
        The matrix profile distance

    See Also
    --------
    mpdist : Compute the z-normalized matrix profile distance (MPdist) measure
        between any two time series
    gpu_mpdist : Compute the z-normalized matrix profile distance (MPdist) measure
        between any two time series with one or more GPU devices

    Notes
    -----
    `DOI: 10.1109/ICDM.2018.00119 \
    <https://www.cs.ucr.edu/~eamonn/MPdist_Expanded.pdf>`__

    See Section III

    Examples
    --------
    >>> from dask.distributed import Client
    >>> if __name__ == "__main__":
    ...     with Client() as dask_client:
    ...         stumpy.mpdisted(
    ...             dask_client,
    ...             np.array([-11.1, 23.4, 79.5, 1001.0]),
    ...             np.array([584., -11., 23., 79., 1001., 0., -19.]),
    ...             m=3)
    0.00019935236191097894
    """
    MPdist = _mpdist(T_A, T_B, m, percentage, k, client=client, mp_func=stumped)

    return MPdist
