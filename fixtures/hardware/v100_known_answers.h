#ifndef UNIFIER_V100_KNOWN_ANSWERS_H
#define UNIFIER_V100_KNOWN_ANSWERS_H

#include <stdint.h>

#ifdef V100_TENSOR
enum { SIDE = 16, COUNT = SIDE * SIDE };
#else
enum { SIDE = 4, COUNT = SIDE * SIDE };
#endif

struct v100_input {
    double fp64[COUNT];
    float fp32[COUNT];
    uint32_t integer[COUNT];
    uint16_t left[COUNT];
    uint16_t right[COUNT];
};

struct v100_output {
    double fp64[COUNT];
    float fp32[COUNT];
    float matrix[COUNT];
    uint32_t integer[COUNT];
};

#endif
