# STUMPY
# Copyright 2019 TD Ameritrade. Released under the terms of the 3-Clause BSD license.
# STUMPY is a trademark of TD Ameritrade IP Company, Inc. All rights reserved.

import numpy as np
from numba import njit, prange
import numba

from . import core, config
from .aamp import aamp

@njit(
    # "(f8[:], f8[:], i8, f8[:], f8[:], f8[:], f8[:], f8[:], f8[:], f8[:], f8[:],"
    # "b1[:], b1[:], b1[:], b1[:], i8[:], i8, i8, i8, f8[:, :, :], f8[:, :],"
    # "f8[:, :], i8[:, :, :], i8[:, :], i8[:, :], b1)",
    fastmath=True,
)
def _compute_diagonal(
    T_A,
    T_B,
    m,
    M_T,
    μ_Q,
    Σ_T_inverse,
    σ_Q_inverse,
    cov_a,
    cov_b,
    cov_c,
    cov_d,
    T_A_subseq_isfinite,
    T_B_subseq_isfinite,
    T_A_subseq_isconstant,
    T_B_subseq_isconstant,
    diags,
    diags_start_idx,
    diags_stop_idx,
    thread_idx,
    ρ,
    ρL,
    ρR,
    I,
    IL,
    IR,
    ignore_trivial,
    index_dilated,
    d
):
    """
    Compute (Numba JIT-compiled) and update the (top-k) Pearson correlation (ρ),
    ρL, ρR, I, IL, and IR sequentially along individual diagonals using a single
    thread and avoiding race conditions.

    Parameters
    ----------
    T_A : numpy.ndarray
        The time series or sequence for which to compute the matrix profile

    T_B : numpy.ndarray
        The time series or sequence that will be used to annotate T_A. For every
        subsequence in T_A, its nearest neighbor in T_B will be recorded.

    m : int
        Window size

    M_T : numpy.ndarray
        Sliding mean of time series, `T`

    μ_Q : numpy.ndarray
        Mean of the query sequence, `Q`, relative to the current sliding window

    Σ_T_inverse : numpy.ndarray
        Inverse sliding standard deviation of time series, `T`

    σ_Q_inverse : numpy.ndarray
        Inverse standard deviation of the query sequence, `Q`, relative to the current
        sliding window

    cov_a : numpy.ndarray
        The first covariance term relating T_A[i + g + m - 1] and M_T_m_1[i + g]

    cov_b : numpy.ndarray
        The second covariance term relating T_B[i + m - 1] and μ_Q_m_1[i]

    cov_c : numpy.ndarray
        The third covariance term relating T_A[i + g - 1] and M_T_m_1[i + g]

    cov_d : numpy.ndarray
        The fourth covariance term relating T_B[i - 1] and μ_Q_m_1[i]

    μ_Q_m_1 : numpy.ndarray
        Mean of the query sequence, `Q`, relative to the current sliding window and
        using a window size of `m-1`

    T_A_subseq_isfinite : numpy.ndarray
        A boolean array that indicates whether a subsequence in `T_A` contains a
        `np.nan`/`np.inf` value (False)

    T_B_subseq_isfinite : numpy.ndarray
        A boolean array that indicates whether a subsequence in `T_B` contains a
        `np.nan`/`np.inf` value (False)

    T_A_subseq_isconstant : numpy.ndarray
        A boolean array that indicates whether a subsequence in `T_A` is constant (True)

    T_B_subseq_isconstant : numpy.ndarray
        A boolean array that indicates whether a subsequence in `T_B` is constant (True)

    diags : numpy.ndarray
        The diagonal indices

    diags_start_idx : int
        The starting (inclusive) diagonal index

    diags_stop_idx : int
        The stopping (exclusive) diagonal index

    thread_idx : int
        The thread index

    ρ : numpy.ndarray
        The (top-k) Pearson correlations, sorted in ascending order per row

    ρL : numpy.ndarray
        The top-1 left Pearson correlations

    ρR : numpy.ndarray
        The top-1 right Pearson correlations

    I : numpy.ndarray
        The (top-k) matrix profile indices

    IL : numpy.ndarray
        The top-1 left matrix profile indices

    IR : numpy.ndarray
        The top-1 right matrix profile indices

    ignore_trivial : bool
        Set to `True` if this is a self-join. Otherwise, for AB-join, set this to
        `False`. Default is `True`.

    Returns
    -------
    None

    Notes
    -----
    `DOI: 10.1007/s10115-017-1138-x \
    <https://www.cs.ucr.edu/~eamonn/ten_quadrillion.pdf>`__

    See Section 4.5

    The above reference outlines a general approach for traversing the distance
    matrix in a diagonal fashion rather than in a row-wise fashion.

    `DOI: 10.1145/3357223.3362721 \
    <https://www.cs.ucr.edu/~eamonn/public/GPU_Matrix_profile_VLDB_30DraftOnly.pdf>`__

    See Section 3.1 and Section 3.3

    The above reference outlines the use of the Pearson correlation via Welford's
    centered sum-of-products along each diagonal of the distance matrix in place of the
    sliding window dot product found in the original STOMP method.
    """
    n_A = T_A.shape[0] # length TS A
    n_B = T_B.shape[0] # length TS B
    m_inverse = 1.0 / m # inverse window length
    constant = (m - 1) * m_inverse * m_inverse  # (m - 1)/(m * m)
    uint64_m = np.uint64(m) # window length m as np uint64
    w = (m-1)*d + 1
    last_valid_index_A = n_A - w
    excl_zone = int(np.ceil(w / config.STUMPY_EXCL_ZONE_DENOM)) 

    # for each diagonal
    for diag_idx in range(diags_start_idx, diags_stop_idx):
        g = diags[diag_idx]

        if g >= 0:
            iter_range = range(0, min(n_A - m + 1, n_B - m + 1 - g))
        else:
            iter_range = range(-g, min(n_A - m + 1, n_B - m + 1 - g))

        # for each position in the diagonal
        for i in iter_range:
            uint64_i = np.uint64(i) # horizontal index
            uint64_j = np.uint64(i + g) # vertical index

            if uint64_i == 0 or uint64_j == 0: # wenn QT_start, berechne dot product, ansonsten nutze QT_i-1,j-1
                cov = (
                    np.dot(
                        (T_B[uint64_j : uint64_j + uint64_m] - M_T[uint64_j]),
                        (T_A[uint64_i : uint64_i + uint64_m] - μ_Q[uint64_i]),
                    )
                    * m_inverse
                )
            else:
                # The next lines are equivalent and left for reference
                # cov = cov + constant * (
                #     (T_B[i + g + m - 1] - M_T_m_1[i + g])
                #     * (T_A[i + m - 1] - μ_Q_m_1[i])
                #     - (T_B[i + g - 1] - M_T_m_1[i + g]) * (T_A[i - 1] - μ_Q_m_1[i])
                # )
                cov = cov + constant * (
                    cov_a[uint64_j] * cov_b[uint64_i]
                    - cov_c[uint64_j] * cov_d[uint64_i]
                )


            if T_B_subseq_isfinite[uint64_j] and T_A_subseq_isfinite[uint64_i]:
                # Neither subsequence contains NaNs
                if T_B_subseq_isconstant[uint64_j] or T_A_subseq_isconstant[uint64_i]:
                    pearson = 0.5
                else:
                    pearson = cov * Σ_T_inverse[uint64_j] * σ_Q_inverse[uint64_i] # calculate distance

                if T_B_subseq_isconstant[uint64_j] and T_A_subseq_isconstant[uint64_i]:
                    pearson = 1.0

                # Remap Index 
                uint64_i_fixed = np.uint64(index_dilated[uint64_i]) # find startindex of subsequence in original TS
                uint64_j_fixed = np.uint64(index_dilated[uint64_j]) # find startindex of subsequence in original TS

                if uint64_i_fixed > last_valid_index_A or uint64_j_fixed > last_valid_index_A: # skip invalid indices (invalid subsequences produced from the dilation mapping)
                    continue

                if ignore_trivial and np.abs(np.int64(uint64_i_fixed)-np.int64(uint64_j_fixed)) <= excl_zone: # skip subsequence pairs that are in the exclusion zone
                    continue

                # `ρ[thread_idx, i, :]` is sorted ascendingly and MUST be updated
                # when the newly-calculated `pearson` value becomes greater than the
                # first (i.e. smallest) element in this array. Note that a higher
                # pearson value corresponds to a lower distance.
                if pearson > ρ[thread_idx, uint64_i_fixed, 0]: # update if distance is lower at i
                    idx = np.searchsorted(ρ[thread_idx, uint64_i_fixed], pearson)
                    
                    core._shift_insert_at_index(
                        ρ[thread_idx, uint64_i_fixed], idx, pearson, shift="left" # insert distance in ρ
                    )
                    core._shift_insert_at_index(
                        I[thread_idx, uint64_i_fixed], idx, uint64_j_fixed, shift="left" # insert NN-Index in I
                    )

                if ignore_trivial:  # self-joins only
                    if pearson > ρ[thread_idx, uint64_j_fixed, 0]: # update if lower at j too (because of the diagonal symmetry if A = B (self joins): DT_0,2 = DT_2,0)
                        idx = np.searchsorted(ρ[thread_idx, uint64_j_fixed], pearson)
                        core._shift_insert_at_index(
                            ρ[thread_idx, uint64_j_fixed], idx, pearson, shift="left"
                        )
                        core._shift_insert_at_index(
                            I[thread_idx, uint64_j_fixed], idx, uint64_i_fixed, shift="left"
                        )

                    if uint64_i_fixed != uint64_j_fixed:
                        # left pearson correlation and left matrix profile index
                        left_idx = min(uint64_i_fixed, uint64_j_fixed)
                        right_idx = max(uint64_i_fixed, uint64_j_fixed)
                        if pearson > ρL[thread_idx, right_idx]:
                            ρL[thread_idx, right_idx] = pearson
                            IL[thread_idx, right_idx] = left_idx
                        # right pearson correlation and right matrix profile index
                        if pearson > ρR[thread_idx, left_idx]:
                            ρR[thread_idx, left_idx] = pearson
                            IR[thread_idx, left_idx] = right_idx

    return

@njit(
    # "(f8[:], f8[:], i8, f8[:], f8[:], f8[:], f8[:], f8[:], f8[:], b1[:], b1[:],"
    # "b1[:], b1[:], i8[:], b1, i8)",
    parallel=True,
    fastmath=True,
)
def _stump(
    T_A,
    T_B,
    m,
    M_T,
    μ_Q,
    Σ_T_inverse,
    σ_Q_inverse,
    M_T_m_1,
    μ_Q_m_1,
    T_A_subseq_isfinite,
    T_B_subseq_isfinite,
    T_A_subseq_isconstant,
    T_B_subseq_isconstant,
    diags,
    ignore_trivial,
    k,
    index_dilated,
    d
):
    """
    A Numba JIT-compiled version of STOMPopt with Pearson correlations for parallel
    computation of the (top-k) matrix profile, the (top-k) matrix profile indices,
    the top-1 left matrix profile and its matrix profile index, and the top-1 right
    matrix profile and its matrix profile index.

    Parameters
    ----------
    T_A : numpy.ndarray
        The time series or sequence for which to compute the matrix profile

    T_B : numpy.ndarray
        The time series or sequence that will be used to annotate T_A. For every
        subsequence in T_A, its nearest neighbor in T_B will be recorded.

    m : int
        Window size

    M_T : numpy.ndarray
        Sliding mean of time series, `T`

    μ_Q : numpy.ndarray
        Mean of the query sequence, `Q`, relative to the current sliding window

    Σ_T_inverse : numpy.ndarray
        Inverse sliding standard deviation of time series, `T`

    σ_Q_inverse : numpy.ndarray
        Inverse standard deviation of the query sequence, `Q`, relative to the current
        sliding window

    M_T_m_1 : numpy.ndarray
        Sliding mean of time series, `T`, using a window size of `m-1`

    μ_Q_m_1 : numpy.ndarray
        Mean of the query sequence, `Q`, relative to the current sliding window and
        using a window size of `m-1`

    T_A_subseq_isfinite : numpy.ndarray
        A boolean array that indicates whether a subsequence in `T_A` contains a
        `np.nan`/`np.inf` value (False)

    T_B_subseq_isfinite : numpy.ndarray
        A boolean array that indicates whether a subsequence in `T_B` contains a
        `np.nan`/`np.inf` value (False)

    T_A_subseq_isconstant : numpy.ndarray
        A boolean array that indicates whether a subsequence in `T_A` is constant (True)

    T_B_subseq_isconstant : numpy.ndarray
        A boolean array that indicates whether a subsequence in `T_B` is constant (True)

    diags : numpy.ndarray
        The diagonal indices

    ignore_trivial : bool
        Set to `True` if this is a self-join. Otherwise, for AB-join, set this to
        `False`. Default is `True`.

    k : int
        The number of top `k` smallest distances used to construct the matrix profile.
        Note that this will increase the total computational time and memory usage
        when k > 1.

    Returns
    -------
    out1 : numpy.ndarray
        The (top-k) matrix profile

    out2 : numpy.ndarray
        The (top-1) left matrix profile

    out3 : numpy.ndarray
        The (top-1) right matrix profile

    out4 : numpy.ndarray
        The (top-k) matrix profile indices

    out5 : numpy.ndarray
        The (top-1) left matrix profile indices

    out6 : numpy.ndarray
        The (top-1) right matrix profile indices

    Notes
    -----
    `DOI: 10.1007/s10115-017-1138-x \
    <https://www.cs.ucr.edu/~eamonn/ten_quadrillion.pdf>`__

    See Section 4.5

    The above reference outlines a general approach for traversing the distance
    matrix in a diagonal fashion rather than in a row-wise fashion.

    `DOI: 10.1145/3357223.3362721 \
    <https://www.cs.ucr.edu/~eamonn/public/GPU_Matrix_profile_VLDB_30DraftOnly.pdf>`__

    See Section 3.1 and Section 3.3

    The above reference outlines the use of the Pearson correlation via Welford's
    centered sum-of-products along each diagonal of the distance matrix in place of the
    sliding window dot product found in the original STOMP method.

    `DOI: 10.1109/ICDM.2016.0085 \
    <https://www.cs.ucr.edu/~eamonn/STOMP_GPU_final_submission_camera_ready.pdf>`__

    See Table II

    Timeseries, T_A, will be annotated with the distance location
    (or index) of all its subsequences in another times series, T_B.

    Return: For every subsequence, Q, in T_A, you will get a distance
    and index for the closest subsequence in T_B. Thus, the array
    returned will have length T_A.shape[0]-m+1. Additionally, the
    left and right matrix profiles are also returned.

    Note: Unlike in the Table II where T_A.shape is expected to be equal
    to T_B.shape, this implementation is generalized so that the shapes of
    T_A and T_B can be different. In the case where T_A.shape == T_B.shape,
    then our algorithm reduces down to the same algorithm found in Table II.

    Additionally, unlike STAMP where the exclusion zone is m/2, the default
    exclusion zone for STOMP is m/4 (See Definition 3 and Figure 3).

    For self-joins, set `ignore_trivial = True` in order to avoid the
    trivial match.

    Note that left and right matrix profiles are only available for self-joins.
    """
    n_A = T_A.shape[0] # length A
    n_B = T_B.shape[0] # length B
    l = n_A - ((m-1)*d + 1) + 1 # number of subsequences in A (was n_A - m + 1, but m is now the window coverage with dilation)
    n_threads = numba.config.NUMBA_NUM_THREADS # default: num threads = num of CPU cores available (for gruenau8 36*2=72)

    ρ = np.full((n_threads, l, k), np.NINF, dtype=np.float64) # init Pearson correlation matrix
    I = np.full((n_threads, l, k), -1, dtype=np.int64) # init MPIndex matrix

    ρL = np.full((n_threads, l), np.NINF, dtype=np.float64) # init Pearson correlation matrix left
    IL = np.full((n_threads, l), -1, dtype=np.int64) # init MPIndex matrix left

    ρR = np.full((n_threads, l), np.NINF, dtype=np.float64) # init Pearson correlation matrix right
    IR = np.full((n_threads, l), -1, dtype=np.int64) # init MPIndex matrix right

    ndist_counts = core._count_diagonal_ndist(diags, m, n_A, n_B) # the number of distances that would be computed for each diagonal index referenced in `diags`
    diags_ranges = core._get_array_ranges(ndist_counts, n_threads, False) # splits ndist_counts into n_threads parts


    cov_a = T_B[m - 1 :] - M_T_m_1[:-1] 
    cov_b = T_A[m - 1 :] - μ_Q_m_1[:-1]
    # The next lines are equivalent and left for reference
    # cov_c = np.roll(T_A, 1)
    # cov_ = cov_c[:M_T_m_1.shape[0]] - M_T_m_1[:]
    cov_c = np.empty(M_T_m_1.shape[0], dtype=np.float64)
    cov_c[1:] = T_B[: M_T_m_1.shape[0] - 1]
    cov_c[0] = T_B[-1]
    cov_c[:] = cov_c - M_T_m_1
    # The next lines are equivalent and left for reference
    # cov_d = np.roll(T_B, 1)
    # cov_d = cov_d[:μ_Q_m_1.shape[0]] - μ_Q_m_1[:]
    cov_d = np.empty(μ_Q_m_1.shape[0], dtype=np.float64)
    cov_d[1:] = T_A[: μ_Q_m_1.shape[0] - 1]
    cov_d[0] = T_A[-1]
    cov_d[:] = cov_d - μ_Q_m_1

    # every thread gets a part of the time series and calculates I, IL, IR, ρ, ρL, ρR
    for thread_idx in prange(n_threads):
        # Compute and update pearson correlations and matrix profile indices
        # within a single thread and avoiding race conditions
        _compute_diagonal(
            T_A,
            T_B,
            m,
            M_T,
            μ_Q,
            Σ_T_inverse,
            σ_Q_inverse,
            cov_a,
            cov_b,
            cov_c,
            cov_d,
            T_A_subseq_isfinite,
            T_B_subseq_isfinite,
            T_A_subseq_isconstant,
            T_B_subseq_isconstant,
            diags,
            diags_ranges[thread_idx, 0], # diags_start_idx
            diags_ranges[thread_idx, 1], # diags_stop_idx
            thread_idx,
            ρ,
            ρL,
            ρR,
            I,
            IL,
            IR,
            ignore_trivial,
            index_dilated,
            d
        )


    # Reduction of results from all threads
    for thread_idx in range(1, n_threads):
        # update top-k arrays
        core._merge_topk_ρI(ρ[0], ρ[thread_idx], I[0], I[thread_idx])

        # update left matrix profile and matrix profile indices
        mask = ρL[0] < ρL[thread_idx]
        ρL[0][mask] = ρL[thread_idx][mask]
        IL[0][mask] = IL[thread_idx][mask]

        # update right matrix profile and matrix profile indices
        mask = ρR[0] < ρR[thread_idx]
        ρR[0][mask] = ρR[thread_idx][mask]
        IR[0][mask] = IR[thread_idx][mask]

    # Reverse top-k rho (and its associated I) to be in descending order and
    # then convert from Pearson correlations to Euclidean distances (ascending order)
    p_norm = np.abs(2 * m * (1 - ρ[0, :, ::-1]))
    I = I[0, :, ::-1]

    p_norm_L = np.abs(2 * m * (1 - ρL[0, :]))
    p_norm_R = np.abs(2 * m * (1 - ρR[0, :]))

    for i in prange(p_norm.shape[0]):
        for j in range(p_norm.shape[1]):
            if p_norm[i, j] < config.STUMPY_P_NORM_THRESHOLD:
                p_norm[i, j] = 0.0

        if p_norm_L[i] < config.STUMPY_P_NORM_THRESHOLD:
            p_norm_L[i] = 0.0

        if p_norm_R[i] < config.STUMPY_P_NORM_THRESHOLD:
            p_norm_R[i] = 0.0

    return (
        np.sqrt(p_norm),
        np.sqrt(p_norm_L),
        np.sqrt(p_norm_R),
        I,
        IL[0],
        IR[0],
    )

def dilation_mapping(X: np.ndarray, d: int) -> np.ndarray:
    result = X[0::d]
    for i in range(1, d):
        next = X[i::d]
        result = np.concatenate((result, next))
    return result

# @core.non_normalized(aamp)
def stump_dil(T_A, m, T_B=None, ignore_trivial=True, normalize=True, p=2.0, k=1, d=1):
    """
    Compute the z-normalized matrix profile

    This is a convenience wrapper around the Numba JIT-compiled parallelized
    `_stump` function which computes the (top-k) matrix profile according to
    STOMPopt with Pearson correlations.

    Parameters
    ----------
    T_A : numpy.ndarray
        The time series or sequence for which to compute the matrix profile

    m : int
        Window size

    T_B : numpy.ndarray, default None
        The time series or sequence that will be used to annotate T_A. For every
        subsequence in T_A, its nearest neighbor in T_B will be recorded. Default is
        `None` which corresponds to a self-join.

    ignore_trivial : bool, default True
        Set to `True` if this is a self-join. Otherwise, for AB-join, set this
        to `False`. Default is `True`.

    normalize : bool, default True
        When set to `True`, this z-normalizes subsequences prior to computing distances.
        Otherwise, this function gets re-routed to its complementary non-normalized
        equivalent set in the `@core.non_normalized` function decorator.

    p : float, default 2.0
        The p-norm to apply for computing the Minkowski distance. This parameter is
        ignored when `normalize == True`.

    k : int, default 1
        The number of top `k` smallest distances used to construct the matrix profile.
        Note that this will increase the total computational time and memory usage
        when k > 1. If you have access to a GPU device, then you may be able to
        leverage `gpu_stump` for better performance and scalability.

    Returns
    -------
    out : numpy.ndarray
        When k = 1 (default), the first column consists of the matrix profile,
        the second column consists of the matrix profile indices, the third column
        consists of the left matrix profile indices, and the fourth column consists
        of the right matrix profile indices. However, when k > 1, the output array
        will contain exactly 2 * k + 2 columns. The first k columns (i.e., out[:, :k])
        consists of the top-k matrix profile, the next set of k columns
        (i.e., out[:, k:2k]) consists of the corresponding top-k matrix profile
        indices, and the last two columns (i.e., out[:, 2k] and out[:, 2k+1] or,
        equivalently, out[:, -2] and out[:, -1]) correspond to the top-1 left
        matrix profile indices and the top-1 right matrix profile indices, respectively.

    See Also
    --------
    stumpy.stumped : Compute the z-normalized matrix profile with a distributed dask
        cluster
    stumpy.gpu_stump : Compute the z-normalized matrix profile with one or more GPU
        devices
    stumpy.scrump : Compute an approximate z-normalized matrix profile

    Notes
    -----
    `DOI: 10.1007/s10115-017-1138-x \
    <https://www.cs.ucr.edu/~eamonn/ten_quadrillion.pdf>`__

    See Section 4.5

    The above reference outlines a general approach for traversing the distance
    matrix in a diagonal fashion rather than in a row-wise fashion.

    `DOI: 10.1145/3357223.3362721 \
    <https://www.cs.ucr.edu/~eamonn/public/GPU_Matrix_profile_VLDB_30DraftOnly.pdf>`__

    See Section 3.1 and Section 3.3

    The above reference outlines the use of the Pearson correlation via Welford's
    centered sum-of-products along each diagonal of the distance matrix in place of the
    sliding window dot product found in the original STOMP method.

    `DOI: 10.1109/ICDM.2016.0085 \
    <https://www.cs.ucr.edu/~eamonn/STOMP_GPU_final_submission_camera_ready.pdf>`__

    See Table II

    Timeseries, T_A, will be annotated with the distance location
    (or index) of all its subsequences in another times series, T_B.

    Return: For every subsequence, Q, in T_A, you will get a distance
    and index for the closest subsequence in T_B. Thus, the array
    returned will have length T_A.shape[0]-m+1. Additionally, the
    left and right matrix profiles are also returned.

    Note: Unlike in the Table II where T_A.shape is expected to be equal
    to T_B.shape, this implementation is generalized so that the shapes of
    T_A and T_B can be different. In the case where T_A.shape == T_B.shape,
    then our algorithm reduces down to the same algorithm found in Table II.

    Additionally, unlike STAMP where the exclusion zone is m/2, the default
    exclusion zone for STOMP is m/4 (See Definition 3 and Figure 3).

    For self-joins, set `ignore_trivial = True` in order to avoid the
    trivial match.

    Note that left and right matrix profiles are only available for self-joins.

    Examples
    --------
    >>> import stumpy
    >>> stumpy.stump(np.array([584., -11., 23., 79., 1001., 0., -19.]), m=3)
    array([[0.11633857113691416, 4, -1, 4],
           [2.694073918063438, 3, -1, 3],
           [3.0000926340485923, 0, 0, 4],
           [2.694073918063438, 1, 1, -1],
           [0.11633857113691416, 0, 0, -1]], dtype=object)
    """
    T_A = dilation_mapping(T_A, d)
    index = np.arange(T_A.shape[0])
    index_dilated = dilation_mapping(index, d)

    if T_B is None:
        T_B = T_A
        ignore_trivial = True
    else:
        T_B = dilation_mapping(T_B, d)

    (
        T_A, # Time Series A
        μ_Q, # Sliding Mean from A with window length m
        σ_Q_inverse, # Inverse sliding standard deviation from A with window length m
        μ_Q_m_1, # Sliding Mean Time Series from A with window length m-1
        T_A_subseq_isfinite,
        T_A_subseq_isconstant,
    ) = core.preprocess_diagonal(T_A, m)

    (
        T_B, # Time Series B
        M_T, # Sliding Mean from B with window length m
        Σ_T_inverse, # Inverse sliding standard deviation from B with window length m
        M_T_m_1, # Sliding Mean Time Series from B with window length m-1
        T_B_subseq_isfinite,
        T_B_subseq_isconstant,
    ) = core.preprocess_diagonal(T_B, m)

    if T_A.ndim != 1:  # pragma: no cover
        raise ValueError(
            f"T_A is {T_A.ndim}-dimensional and must be 1-dimensional. "
            "For multidimensional STUMP use `stumpy.mstump` or `stumpy.mstumped`"
        )

    if T_B.ndim != 1:  # pragma: no cover
        raise ValueError(
            f"T_B is {T_B.ndim}-dimensional and must be 1-dimensional. "
            "For multidimensional STUMP use `stumpy.mstump` or `stumpy.mstumped`"
        )

    core.check_window_size(m, max_size=min(T_A.shape[0], T_B.shape[0]))
    ignore_trivial = core.check_ignore_trivial(T_A, T_B, ignore_trivial)

    n_A = T_A.shape[0]
    n_B = T_B.shape[0]
    l = n_A - ((m-1)*d + 1) + 1 # window coverage = (m-1)*d + 1

    excl_zone = 0 #int(np.ceil(m / config.STUMPY_EXCL_ZONE_DENOM))

    if ignore_trivial:
        diags = np.arange(excl_zone + 1, n_A - m + 1, dtype=np.int64)
    else:
        diags = np.arange(-(n_A - m + 1) + 1, n_B - m + 1, dtype=np.int64)

    P, PL, PR, I, IL, IR = _stump(
        T_A, 
        T_B,
        m,
        M_T,
        μ_Q,
        Σ_T_inverse,
        σ_Q_inverse,
        M_T_m_1,
        μ_Q_m_1,
        T_A_subseq_isfinite,
        T_B_subseq_isfinite,
        T_A_subseq_isconstant,
        T_B_subseq_isconstant,
        diags,
        ignore_trivial,
        k,
        index_dilated,
        d
    )

    out = np.empty((l, 2 * k + 2), dtype=object)  # last two columns are to
    # store left and right matrix profile indices
    out[:, :k] = P
    out[:, k:] = np.column_stack((I, IL, IR))

    core._check_P(out[:, 0])

    return out
