#include <cuda_fp16.h>

#include "v100_known_answers.h"

extern "C" __global__ void v100_known_answers(const v100_input *input, v100_output *output)
{
    unsigned int i = threadIdx.x;
    if (i >= COUNT)
        return;

    output->integer[i] = input->integer[i] * 13u + 29u;
    output->fp32[i] = input->fp32[i] * 1.5f - 2.0f;
    output->fp64[i] = input->fp64[i] * 0.75 + 0.125;

    float sum = 0.0f;
    for (unsigned int k = 0; k < SIDE; ++k) {
        float a = __half2float(__ushort_as_half(input->left[i / SIDE * SIDE + k]));
        float b = __half2float(__ushort_as_half(input->right[k * SIDE + i % SIDE]));
        sum += a * b;
    }
    output->matrix[i] = sum;
}
