#include <cuda_fp16.h>
#ifdef V100_TENSOR
#include <mma.h>
#include <stddef.h>
#endif

#include "v100_known_answers.h"

extern "C" __global__ void v100_known_answers(const v100_input *input, v100_output *output)
{
    unsigned int i = threadIdx.x;
    if (i >= COUNT)
        return;

    output->integer[i] = input->integer[i] * 13u + 29u;
    output->fp32[i] = input->fp32[i] * 1.5f - 2.0f;
    output->fp64[i] = input->fp64[i] * 0.75 + 0.125;

#ifdef V100_TENSOR
    // WMMA uses one full warp and requires 32-byte aligned tile addresses.
    static_assert(offsetof(v100_input, left) % 32 == 0);
    static_assert(offsetof(v100_input, right) % 32 == 0);
    static_assert(offsetof(v100_output, matrix) % 32 == 0);
    if (i < warpSize) {
        using namespace nvcuda::wmma;
        fragment<matrix_a, SIDE, SIDE, SIDE, half, row_major> left;
        fragment<matrix_b, SIDE, SIDE, SIDE, half, row_major> right;
        fragment<accumulator, SIDE, SIDE, SIDE, float> sum;
        load_matrix_sync(left, reinterpret_cast<const half *>(input->left), SIDE);
        load_matrix_sync(right, reinterpret_cast<const half *>(input->right), SIDE);
        fill_fragment(sum, 0.0f);
        mma_sync(sum, left, right, sum);
        store_matrix_sync(output->matrix, sum, SIDE, mem_row_major);
    }
#else
    float sum = 0.0f;
    for (unsigned int k = 0; k < SIDE; ++k) {
        float a = __half2float(__ushort_as_half(input->left[i / SIDE * SIDE + k]));
        float b = __half2float(__ushort_as_half(input->right[k * SIDE + i % SIDE]));
        sum += a * b;
    }
    output->matrix[i] = sum;
#endif
}
