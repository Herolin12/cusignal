# Copyright (c) 2019-2020, NVIDIA CORPORATION.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import cupy as cp

from enum import Enum
from numba import cuda, void, float32, float64


class GPUKernel(Enum):
    PREDICT = 0
    UPDATE = 1


class GPUBackend(Enum):
    CUPY = 0
    NUMBA = 1


# Numba type supported and corresponding C type
_SUPPORTED_TYPES = {
    cp.float32: [float32, "float"],
    cp.float64: [float64, "double"],
}


_numba_kernel_cache = {}
_cupy_kernel_cache = {}


# Use until functionality provided in Numba 0.49/0.50 available
def stream_cupy_to_numba(cp_stream):
    """
    Notes:
        1. The lifetime of the returned Numba stream should be as
           long as the CuPy one, which handles the deallocation
           of the underlying CUDA stream.
        2. The returned Numba stream is assumed to live in the same
           CUDA context as the CuPy one.
        3. The implementation here closely follows that of
           cuda.stream() in Numba.
    """
    from ctypes import c_void_p
    import weakref

    # get the pointer to actual CUDA stream
    raw_str = cp_stream.ptr

    # gather necessary ingredients
    ctx = cuda.devices.get_context()
    handle = c_void_p(raw_str)

    # create a Numba stream
    nb_stream = cuda.cudadrv.driver.Stream(
        weakref.proxy(ctx), handle, finalizer=None
    )

    return nb_stream


def _numba_predict(alpha, x_in, F, P, Q):

    x, y, z = cuda.grid(3)
    _, _, strideZ = cuda.gridsize(3)
    tz = cuda.threadIdx.z

    dim_x = P.shape[1]

    xx_block_size = dim_x * dim_x * cuda.blockDim.z
    xx_idx = dim_x * dim_x * tz

    s_buffer = cuda.shared.array(shape=0, dtype=float32)

    s_A = s_buffer[: (xx_block_size * 1)]
    s_F = s_buffer[(xx_block_size * 1) :]

    x_key = x * dim_x + y

    #  Each i is a different point
    for z_idx in range(z, x_in.shape[0], strideZ):

        s_F[xx_idx + x_key] = F[
            z_idx, x, y,
        ]

        cuda.syncthreads()

        #  Load alpha_sq and Q into registers
        alpha_sq = alpha[
            z_idx, 0, 0,
        ]
        local_Q = Q[
            z_idx, x, y,
        ]

        #  Compute new self.x
        temp: x_in.dtype = 0
        if y == 0:
            for j in range(dim_x):
                temp += s_F[xx_idx + (x * dim_x + j)] * x_in[z_idx, j, y]

            x_in[z_idx, x, y] = temp

        #  Compute dot(self.F, self.P)
        temp: x_in.dtype = 0
        for j in range(dim_x):
            temp += s_F[xx_idx + (x * dim_x + j)] * P[z_idx, j, y]

        s_A[xx_idx + x_key] = temp

        cuda.syncthreads()

        #  Compute dot(dot(self.F, self.P), self.F.T)
        temp: x_in.dtype = 0
        for j in range(dim_x):
            temp += (
                s_A[xx_idx + (x * dim_x + j)] * s_F[xx_idx + (y * dim_x + j)]
            )

        #  Compute alpha^2 * dot(dot(self.F, self.P), self.F.T) + self.Q
        P[z_idx, x, y] = alpha_sq * temp + local_Q


def _numba_update(x_in, z_in, H, P, R):

    x, y, z = cuda.grid(3)
    _, _, strideZ = cuda.gridsize(3)
    tz = cuda.threadIdx.z

    dim_x = P.shape[1]
    dim_z = R.shape[1]

    xx_block_size = dim_x * dim_x * cuda.blockDim.z
    xz_block_size = dim_x * dim_z * cuda.blockDim.z
    zz_block_size = dim_z * dim_z * cuda.blockDim.z
    xx_idx = dim_x * dim_x * tz
    xz_idx = dim_x * dim_z * tz
    zz_idx = dim_z * dim_z * tz

    s_buffer = cuda.shared.array(shape=0, dtype=float32)

    s_A = s_buffer[: (xx_block_size * 1)]
    s_B = s_buffer[(xx_block_size * 1) : (xx_block_size * 2)]
    s_P = s_buffer[(xx_block_size * 2) : (xx_block_size * 3)]
    s_H = s_buffer[(xx_block_size * 3) : (xx_block_size * 3 + xz_block_size)]
    s_K = s_buffer[
        (xx_block_size * 3 + xz_block_size) : (
            xx_block_size * 3 + xz_block_size * 2
        )
    ]
    s_R = s_buffer[
        (xx_block_size * 3 + xz_block_size * 2) : (
            xx_block_size * 3 + xz_block_size * 2 + zz_block_size
        )
    ]
    s_y = s_buffer[(xx_block_size * 3 + xz_block_size * 2 + zz_block_size) :]

    x_key = x * dim_x + y
    z_key = x * dim_z + y

    #  Each i is a different point
    for z_idx in range(z, x_in.shape[0], strideZ):

        s_P[xx_idx + x_key] = P[
            z_idx, x, y,
        ]

        if x < dim_z:
            s_H[xz_idx + x_key] = H[z_idx, x, y]

        if x < dim_z and y < dim_z:
            s_R[zz_idx + z_key] = R[z_idx, x, y]

        cuda.syncthreads()

        #  Compute self.y : z = dot(self.H, self.x)
        temp: x_in.dtype = 0.0
        if x < dim_z and y == 0:
            temp_z: x_in.dtype = z_in[z_idx, x, y]
            for j in range(dim_x):
                temp += s_H[xz_idx + (x * dim_x + j)] * x_in[z_idx, j, y]

            s_y[(dim_z * tz) + x] = temp_z - temp

        #  Compute PHT : dot(self.P, self.H.T)
        temp: x_in.dtype = 0.0
        if y < dim_z:
            for j in range(dim_x):
                temp += (
                    s_P[xx_idx + (x * dim_x + j)]
                    * s_H[xz_idx + (y * dim_x + j)]
                )

            #  s_A holds PHT
            s_A[xx_idx + z_key] = temp

        cuda.syncthreads()

        #  Compute self.S : dot(self.H, PHT) + self.R
        temp: x_in.dtype = 0.0
        if x < dim_z and y < dim_z:
            for j in range(dim_x):
                temp += (
                    s_H[xz_idx + (x * dim_x + j)]
                    * s_A[xx_idx + (j * dim_z + y)]
                )

            #  s_B holds S - system uncertainty
            s_B[xx_idx + z_key] = temp + s_R[zz_idx + z_key]

        cuda.syncthreads()

        if x < dim_z and y < dim_z:

            #  Compute linalg.inv(S)
            #  Hardcoded for 2x2
            sign = 1 if (x + y) % 2 == 0 else -1

            #  sign * determinant
            sign_det = sign * (
                (s_B[xx_idx + (0 * dim_z + 0)] * s_B[xx_idx + (1 * dim_z + 1)])
                - (
                    s_B[xx_idx + (1 * dim_z + 0)]
                    * s_B[xx_idx + (0 * dim_z + 1)]
                )
            )

            #  s_B hold SI - inverse system uncertainty
            temp = (
                s_B[xx_idx + ((dim_z - 1 - x) * dim_z + (dim_z - 1 - y))]
                / sign_det
            )
            s_B[xx_idx + z_key] = temp

        cuda.syncthreads()

        #  Compute self.K : dot(PHT, self.SI)
        #  kalman gain
        temp: x_in.dtype = 0.0
        if y < 2:
            for j in range(dim_z):
                temp += (
                    s_A[xx_idx + (x * dim_z + j)]
                    * s_B[xx_idx + (y * dim_z + j)]
                )
            s_K[xz_idx + z_key] = temp

        cuda.syncthreads()

        #  Compute self.x : self.x + cp.dot(self.K, self.y)
        temp: x_in.dtype = 0.0
        if y == 0:
            for j in range(dim_z):
                temp += s_K[xz_idx + (x * dim_z + j)] * s_y[(dim_z * tz) + j]

            x_in[z_idx, x, y] += temp

        #  Compute I_KH = self_I - dot(self.K, self.H)
        temp: x_in.dtype = 0.0
        for j in range(dim_z):
            temp += (
                s_K[xz_idx + (x * dim_z + j)] * s_H[xz_idx + (j * dim_x + y)]
            )

        #  s_A holds I_KH
        s_A[xx_idx + x_key] = (1.0 if x == y else 0.0) - temp

        cuda.syncthreads()

        #  Compute self.P = dot(dot(I_KH, self.P), I_KH.T) +
        #  dot(dot(self.K, self.R), self.K.T)

        #  Compute dot(I_KH, self.P)
        temp: x_in.dtype = 0.0
        for j in range(dim_x):
            temp += (
                s_A[xx_idx + (x * dim_x + j)] * s_P[xx_idx + (j * dim_x + y)]
            )

        #  s_A holds dot(I_KH, self.P)
        s_B[xx_idx + x_key] = temp

        cuda.syncthreads()

        #  Compute dot(dot(I_KH, self.P), I_KH.T)
        temp: x_in.dtype = 0.0
        for j in range(dim_x):
            temp += (
                s_B[xx_idx + (x * dim_x + j)] * s_A[xx_idx + (y * dim_x + j)]
            )

        #  Compute dot(self.K, self.R)
        temp2: x_in.dtype = 0.0
        if y < dim_z:
            for j in range(dim_z):
                temp2 += (
                    s_K[xz_idx + (x * dim_z + j)]
                    * s_R[zz_idx + (j * dim_z + y)]
                )

        #  s_A holds dot(self.K, self.R)
        s_A[xx_idx + z_key] = temp2

        cuda.syncthreads()

        #  Compute dot(dot(self.K, self.R), self.K.T)
        temp2: x_in.dtype = 0.0
        for j in range(dim_z):
            temp2 += (
                s_A[xx_idx + (x * dim_z + j)] * s_K[xz_idx + (y * dim_z + j)]
            )

        P[z_idx, x, y] = temp + temp2


def _numba_kalman_signature(ty):
    return void(
        ty[:, :, :], ty[:, :, :], ty[:, :, :], ty[:, :, :], ty[:, :, :],
    )


# Custom Cupy raw kernel
# Matthew Nicely - mnicely@nvidia.com
cuda_code = """
template<typename T, int DIM_Z>
__device__ T inverse(
    const int & idx,
    const int & tx,
    const int & ty,
    T * s_B) {

    const int sign { ( ( tx + ty ) % 2 == 0 ) ? 1 : -1 };

    T determinant {};
    T temp {};

    if ( DIM_Z == 2 ) {
        determinant = ( (
            s_B[idx + (0 * DIM_Z + 0)] *
            s_B[idx + (1 * DIM_Z + 1)] ) -
            ( s_B[idx + (1 * DIM_Z + 0)] *
            s_B[idx + (0 * DIM_Z + 1)] ) );

        temp = s_B[idx + (((tx + 1) % DIM_Z) * DIM_Z + ((ty + 1) % DIM_Z))];

        temp /= ( determinant * sign );

    } else if ( DIM_Z == 3 ) {

#pragma unroll DIM_Z
        for (int i = 0; i < DIM_Z; i++) {
            determinant += ( s_B[idx + (0 * DIM_Z + i)] * (
                s_B[idx + (1 * DIM_Z + ((i + 1) % DIM_Z))] *
                s_B[idx + (2 * DIM_Z + ((i + 2) % DIM_Z))] -
                s_B[idx + (1 * DIM_Z + ((i + 2) % DIM_Z))] *
                s_B[idx + (2 * DIM_Z + ((i + 1) % DIM_Z))] ) );
        }

        if (tx==0 && ty ==0 && (static_cast<int>( blockIdx.z * blockDim.z + threadIdx.z ) == 1)) {
            printf("d %f\\n", determinant);
        }

        temp = s_B[idx + (((tx + 1) % DIM_Z) * DIM_Z + ((ty + 1) % DIM_Z))] *
                s_B[idx + (((tx + 2) % DIM_Z) * DIM_Z + ((ty + 2) % DIM_Z))] -
                s_B[idx + (((tx + 1) % DIM_Z) * DIM_Z + ((ty + 2) % DIM_Z))] *
                s_B[idx + (((tx + 2) % DIM_Z) * DIM_Z + ((ty + 1) % DIM_Z))];

        if ((static_cast<int>( blockIdx.z * blockDim.z + threadIdx.z ) == 1)) {
            printf("t %f %d %d\\n", temp, tx, ty);
        }

        temp /= ( determinant * sign );

        if ((static_cast<int>( blockIdx.z * blockDim.z + threadIdx.z ) == 1)) {
            printf("i %f %d %d\\n", temp, tx, ty);
        }

    } else {
        determinant = sign;
    }

    return ( temp );
}


template<typename T, int DIM_X, int MAX_TPB, int MIN_BPSM>
__global__ void __launch_bounds__(MAX_TPB, MIN_BPSM) _cupy_predict(
        const int num_points,
        const T * __restrict__ alpha_sq,
        T * __restrict__ x_in,
        const T * __restrict__ F,
        T * __restrict__ P,
        const T * __restrict__ Q
        ) {

    __shared__ T s_A[DIM_X * DIM_X * 16];
    __shared__ T s_F[DIM_X * DIM_X * 16];

    const int tx { static_cast<int>( blockIdx.x * blockDim.x + threadIdx.x ) };
    const int ty { static_cast<int>( blockIdx.y * blockDim.y + threadIdx.y ) };
    const int tz { static_cast<int>( blockIdx.z * blockDim.z + threadIdx.z ) };

    const int stride_z { static_cast<int>( blockDim.z * gridDim.z ) };

    const int xx_idx { static_cast<int>( DIM_X * DIM_X * threadIdx.z ) };

    const int x_value { ty * DIM_X + tx };

    for ( int tid_z = tz; tid_z < num_points; tid_z += stride_z ) {

        s_F[xx_idx + x_value] = F[tid_z * DIM_X * DIM_X + ty * DIM_X + tx];

        __syncthreads();

        T alpha2 { alpha_sq[tid_z] };
        T localQ { Q[tid_z * DIM_X * DIM_X + ty * DIM_X + tx] };

        T temp {};

        if ( tx == 0 ) {
#pragma unroll DIM_X
            for ( int j = 0; j < DIM_X; j++ ) {
                temp += s_F[xx_idx + (ty * DIM_X + j)] *
                    x_in[tid_z * DIM_X * 1 + j * 1 + tx];
            }
            x_in[tid_z * DIM_X * 1 + ty * 1 + tx] = temp;
        }

        temp = 0.0f;
#pragma unroll DIM_X
        for ( int j = 0; j < DIM_X; j++ ) {
            temp += s_F[xx_idx + (ty * DIM_X + j)] *
                P[tid_z * DIM_X * DIM_X + j * DIM_X + tx];
        }
        s_A[xx_idx + x_value] = temp;

        __syncthreads();

        temp = 0.0f;
#pragma unroll DIM_X
        for ( int j = 0; j < DIM_X; j++ ) {
            temp += s_A[xx_idx + (ty * DIM_X + j)] *
                s_F[xx_idx + (tx * DIM_X + j)];
        }

        P[tid_z * DIM_X * DIM_X + ty * DIM_X + tx] =
            alpha2 * temp + localQ;
    }
}

template<typename T, int DIM_X, int DIM_Z, int MAX_TPB, int MIN_BPSM>
__global__ void __launch_bounds__(MAX_TPB, MIN_BPSM) _cupy_update(
        const int num_points,
        T * __restrict__ x_in,
        const T * __restrict__ z_in,
        const T * __restrict__ H,
        T * __restrict__ P,
        const T * __restrict__ R
        ) {

    __shared__ T s_A[DIM_X * DIM_X * 16];
    __shared__ T s_B[DIM_X * DIM_X * 16];
    __shared__ T s_P[DIM_X * DIM_X * 16];
    __shared__ T s_H[DIM_Z * DIM_X * 16];
    __shared__ T s_K[DIM_X * DIM_Z * 16];
    __shared__ T s_R[DIM_Z * DIM_Z * 16];
    __shared__ T s_y[DIM_Z * 1 * 16];

    const int tx {
        static_cast<int>( blockIdx.x * blockDim.x + threadIdx.x ) };
    const int ty {
        static_cast<int>( blockIdx.y * blockDim.y + threadIdx.y ) };
    const int tz {
        static_cast<int>( blockIdx.z * blockDim.z + threadIdx.z ) };

    const int stride_z { static_cast<int>( blockDim.z * gridDim.z ) };

    const int xx_idx { static_cast<int>( DIM_X * DIM_X * threadIdx.z ) };
    const int xz_idx { static_cast<int>( DIM_X * DIM_Z * threadIdx.z ) };
    const int zz_idx { static_cast<int>( DIM_Z * DIM_Z * threadIdx.z ) };

    const int x_value { ty * DIM_X + tx };
    const int z_value { ty * DIM_Z + tx };

    for ( int tid_z = tz; tid_z < num_points; tid_z += stride_z ) {

        s_P[xx_idx + x_value] = P[tid_z * DIM_X * DIM_X + ty * DIM_X + tx];

        if ( ty < DIM_Z ) {
            s_H[xz_idx + x_value] =
                H[tid_z * DIM_Z * DIM_X + ty * DIM_X + tx];
        }

        if ( ( ty < DIM_Z ) && ( tx < DIM_Z ) ) {
            s_R[zz_idx + z_value] =
                R[tid_z * DIM_Z * DIM_Z + ty * DIM_Z + tx];
        }
        __syncthreads();

        T temp {};

        // Compute self.y : z = dot(self.H, self.x)
        if ( ( tx == 0 ) && ( ty < DIM_Z ) ) {
            T temp_z { z_in[tid_z * DIM_Z * 1 + ty * 1 + tx] };

#pragma unroll DIM_X
            for ( int j = 0; j < DIM_X; j++ ) {
                temp += s_H[xz_idx + (ty * DIM_X + j)] *
                    x_in[tid_z * DIM_X * 1 + j * 1 + tx];
            }

            s_y[threadIdx.z * DIM_Z * 1 + ty * 1 + tx] = temp_z - temp;
        }

        // Compute PHT : dot(self.P, self.H.T)
        temp = 0.0f;
        if ( tx < DIM_Z ) {
#pragma unroll DIM_X
            for ( int j = 0; j < DIM_X; j++ ) {
                temp += s_P[xx_idx + (ty * DIM_X + j)] *
                    s_H[xz_idx + (tx * DIM_X + j)];
            }
            // s_A holds PHT
            s_A[xx_idx + z_value] = temp;
        }

        __syncthreads();

        // Compute self.S : dot(self.H, PHT) + self.R
        temp = 0.0f;
        if ( ( tx < DIM_Z ) && ( ty < DIM_Z ) ) {
#pragma unroll DIM_X
            for ( int j = 0; j < DIM_X; j++ ) {
                temp += s_H[xz_idx + (ty * DIM_X + j)] *
                    s_A[xx_idx + (j * DIM_Z + tx)];
            }
            // s_B holds S - system uncertainty
            s_B[xx_idx + z_value] = temp + s_R[zz_idx + z_value];
        }

        __syncthreads();

        if ( ( tx < DIM_Z ) && ( ty < DIM_Z ) ) {

            //if (tx == 0 && ty == 0 && tz == 0) {
            //    for (int i = 0; i< 9; i++) {
            //        printf("%f\\n", s_B[xx_idx + i]);
            //    }
            //}

            // Compute linalg.inv(S)
            // Hardcoded for 2x2, 3x3
            temp = inverse<T, DIM_Z>( xx_idx, tx, ty, s_B );

            // s_B hold SI - inverse system uncertainty
            s_B[xx_idx + z_value] = temp;
        }

        __syncthreads();

        //  Compute self.K : dot(PHT, self.SI)
        //  kalman gain
        temp = 0.0f;
        if ( tx < 2 ) {
#pragma unroll DIM_Z
            for ( int j = 0; j < DIM_Z; j++ ) {
                temp += s_A[xx_idx + (ty * DIM_Z + j)] *
                    s_B[xx_idx + (tx * DIM_Z + j)];
            }
            s_K[xz_idx + z_value] = temp;
        }

        __syncthreads();

        //  Compute self.x : self.x + cp.dot(self.K, self.y)
        temp = 0.0;
        if ( tx == 0 ) {
#pragma unroll DIM_Z
            for ( int j = 0; j < DIM_Z; j++ ) {
                temp += s_K[xz_idx + (ty * DIM_Z + j)] *
                    s_y[threadIdx.z * DIM_Z + j];
            }
            x_in[tid_z * DIM_X * 1 + ty * 1 + tx] += temp;
        }

        // Compute I_KH = self_I - dot(self.K, self.H)
        temp = 0.0f;
#pragma unroll DIM_Z
        for ( int j = 0; j < DIM_Z; j++ ) {
            temp += s_K[xz_idx + (ty * DIM_Z + j)] *
                s_H[xz_idx + (j * DIM_X + tx)];
        }
        // s_A holds I_KH
        s_A[xx_idx + x_value] = ( ( tx == ty ) ? 1 : 0 ) - temp;

        __syncthreads();

        // Compute self.P = dot(dot(I_KH, self.P), I_KH.T) +
        // dot(dot(self.K, self.R), self.K.T)

        // Compute dot(I_KH, self.P)
        temp = 0.0f;
#pragma unroll DIM_X
        for ( int j = 0; j < DIM_X; j++ ) {
            temp += s_A[xx_idx + (ty * DIM_X + j)] *
                s_P[xx_idx + (j * DIM_X + tx)];
        }
        s_B[xx_idx + x_value] = temp;

        __syncthreads();

        // Compute dot(dot(I_KH, self.P), I_KH.T)
        temp = 0.0f;
#pragma unroll DIM_X
        for ( int j = 0; j < DIM_X; j++ ) {
            temp += s_B[xx_idx + (ty * DIM_X + j)] *
                s_A[xx_idx + (tx * DIM_X + j)];
        }

        s_P[xx_idx + (ty * DIM_X + tx)] = temp;

        temp = 0.0f;
        if ( tx < DIM_Z ) {
#pragma unroll DIM_Z
            for ( int j = 0; j < DIM_Z; j++ ) {
                temp += s_K[xz_idx + (ty * DIM_Z + j)] *
                    s_R[zz_idx + (j * DIM_Z + tx)];
            }
        }

        // s_A holds dot(self.K, self.R)
        s_A[xx_idx + z_value] = temp;

        __syncthreads();

        temp = 0.0f;
#pragma unroll DIM_Z
        for ( int j = 0; j < DIM_Z; j++ ) {
            temp += s_A[xx_idx + (ty * DIM_Z + j)] *
                s_K[xz_idx + (tx * DIM_Z + j)];
        }

        P[tid_z * DIM_X * DIM_X + ty * DIM_X + tx] =
            s_P[xx_idx + (ty * DIM_X + tx)] + temp;
    }
}

"""


class _cupy_predict_wrapper(object):
    def __init__(self, grid, block, stream, kernel):
        if isinstance(grid, int):
            grid = (grid,)
        if isinstance(block, int):
            block = (block,)

        self.grid = grid
        self.block = block
        self.stream = stream
        self.kernel = kernel

    def __call__(
        self, alpha_sq, x, F, P, Q,
    ):

        kernel_args = (x.shape[0], alpha_sq, x, F, P, Q)

        self.stream.use()
        self.kernel(self.grid, self.block, kernel_args)


class _cupy_update_wrapper(object):
    def __init__(self, grid, block, stream, kernel):
        if isinstance(grid, int):
            grid = (grid,)
        if isinstance(block, int):
            block = (block,)

        self.grid = grid
        self.block = block
        self.stream = stream
        self.kernel = kernel

    def __call__(self, x, z, H, P, R):

        kernel_args = (x.shape[0], x, z, H, P, R)

        self.stream.use()
        self.kernel(self.grid, self.block, kernel_args)


def _get_backend_kernel(dtype, grid, block, smem, stream, use_numba, k_type):

    if not use_numba:
        kernel = _cupy_kernel_cache[(dtype.name, k_type)]
        if kernel:
            if k_type == GPUKernel.PREDICT:
                return _cupy_predict_wrapper(grid, block, stream, kernel)
            elif k_type == GPUKernel.UPDATE:
                return _cupy_update_wrapper(grid, block, stream, kernel)
            else:
                raise NotImplementedError(
                    "No CuPY kernel found for k_type {}, datatype {}".format(
                        k_type, dtype
                    )
                )
        else:
            raise ValueError(
                "Kernel {} not found in _cupy_kernel_cache".format(k_type)
            )
    else:
        nb_stream = stream_cupy_to_numba(stream)
        kernel = _numba_kernel_cache[(dtype.name, k_type)]

        if kernel:
            return kernel[grid, block, nb_stream, smem]
        else:
            raise ValueError(
                "Kernel {} not found in _numba_kernel_cache".format(k_type)
            )
    raise NotImplementedError(
        "No kernel found for k_type {}, datatype {}".format(k_type, dtype.name)
    )


def _populate_kernel_cache(
    np_type, use_numba, dim_x, dim_z, max_tpb, min_bpsm
):

    # Check in np_type is a supported option
    try:
        numba_type, c_type = _SUPPORTED_TYPES[np_type]

    except ValueError:
        raise Exception("No kernel found for datatype {}".format(np_type))

    if not use_numba:
        # Instantiate the cupy kernel for this type and compile
        specializations = (
            "_cupy_predict<{}, {}, {}, {}>".format(
                c_type, dim_x, max_tpb, min_bpsm
            ),
            "_cupy_update<{}, {}, {}, {}, {}>".format(
                c_type, dim_x, dim_z, max_tpb, min_bpsm
            ),
        )
        module = cp.RawModule(
            code=cuda_code,
            options=("-std=c++11", "-use_fast_math"),
            specializations=specializations,
        )
        kernels = [module.get_mangled_name(ker) for ker in specializations]

        _cupy_kernel_cache[
            (str(numba_type), GPUKernel.PREDICT)
        ] = module.get_function(kernels[0])
        _cupy_kernel_cache[
            (str(numba_type), GPUKernel.UPDATE)
        ] = module.get_function(kernels[1])
    else:
        sig = _numba_kalman_signature(numba_type)
        _numba_kernel_cache[(str(numba_type), GPUKernel.PREDICT)] = cuda.jit(
            sig, fastmath=True
        )(_numba_predict)
        _numba_kernel_cache[(str(numba_type), GPUKernel.UPDATE)] = cuda.jit(
            sig, fastmath=True
        )(_numba_update)
