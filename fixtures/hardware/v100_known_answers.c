#define _GNU_SOURCE
#include <cuda.h>
#include <dirent.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "v100_known_answers.h"

static int checked(const char *call, CUresult code)
{
    if (code == CUDA_SUCCESS)
        return 1;

    const char *name = NULL;
    cuGetErrorName(code, &name);
    fprintf(stderr, "%s: %s (%d)\n", call, name ? name : "unknown CUDA error", code);
    return 0;
}

#define CUDA(call) do { if (!checked(#call, (call))) goto cleanup; } while (0)

static int verify_device_nodes(const char *selected)
{
    if (strncmp(selected, "nvidia", 6) != 0 || strlen(selected) != 7
        || (selected[6] != '0' && selected[6] != '1'))
        return 0;

    DIR *directory = opendir("/dev");
    if (!directory)
        return 0;

    struct dirent *entry;
    int found = 0;
    while ((entry = readdir(directory))) {
        const char *name = entry->d_name;
        if (strncmp(name, "nvidia", 6) == 0 && name[6]
            && strspn(name + 6, "0123456789") == strlen(name + 6)) {
            if (strcmp(name, selected) != 0) {
                found = 0;
                break;
            }
            int fd = openat(dirfd(directory), name, O_RDWR | O_CLOEXEC | O_NOFOLLOW);
            struct stat info;
            found = fd >= 0 && fstat(fd, &info) == 0 && S_ISCHR(info.st_mode);
            if (fd >= 0)
                close(fd);
        }
    }
    closedir(directory);
    return found;
}

static void uuid_text(const CUuuid *id, char output[41])
{
    char *next = stpcpy(output, "GPU-");
    for (int i = 0; i < 16; ++i) {
        if (i == 4 || i == 6 || i == 8 || i == 10)
            *next++ = '-';
        next += sprintf(next, "%02x", (unsigned char)id->bytes[i]);
    }
}

static int left_index(int i) { return (i * 3 + 1) % 7; }
static int right_index(int i) { return (i + i / SIDE + 2) % 7; }

static void make_input(struct v100_input *input)
{
    const uint16_t half[] = {0x0000, 0x3800, 0xb800, 0x3c00, 0xbc00, 0x4000, 0xc000};
    for (int i = 0; i < COUNT; ++i) {
        input->integer[i] = UINT32_C(0xfffffff0) + (uint32_t)i;
        input->fp32[i] = (float)(i - 3) / 4.0f;
        input->fp64[i] = (double)(i - 7) / 32.0;
        input->left[i] = half[left_index(i)];
        input->right[i] = half[right_index(i)];
    }
}

static int correct(const struct v100_input *input, const struct v100_output *output)
{
    const double values[] = {0.0, 0.5, -0.5, 1.0, -1.0, 2.0, -2.0};
    for (int i = 0; i < COUNT; ++i) {
        double reference = 0.0;
        for (int k = 0; k < SIDE; ++k)
            reference += values[left_index(i / SIDE * SIDE + k)]
                       * values[right_index(k * SIDE + i % SIDE)];
        /* The inputs and results are exactly representable in each format. */
        if (output->integer[i] != input->integer[i] * 13u + 29u
            || output->fp32[i] != input->fp32[i] * 1.5f - 2.0f
            || output->fp64[i] != input->fp64[i] * 0.75 + 0.125
            || output->matrix[i] != reference) {
            fprintf(stderr, "CUDA result mismatch at element %d\n", i);
            return 0;
        }
    }
    return 1;
}

int main(int argc, char **argv)
{
    CUcontext context = NULL;
    CUmodule module = NULL;
    CUdeviceptr device_input = 0, device_output = 0;
    int passed = 0;
    if (argc != 5) {
        fprintf(stderr, "usage: %s UUID PCI_BDF nvidiaN IMAGE.cubin\n", argv[0]);
        return 2;
    }
    if (getenv("CUDA_VISIBLE_DEVICES") || !verify_device_nodes(argv[3])) {
        fprintf(stderr, "CUDA visibility masking or unexpected NVIDIA device nodes\n");
        return 2;
    }
    Dl_info driver_lib;
    if (!dladdr((void *)cuInit, &driver_lib)
        || strcmp(driver_lib.dli_fname, "/run/unifier-cuda/libcuda.so.1") != 0) {
        fprintf(stderr, "unexpected CUDA driver library path\n");
        return 2;
    }

    int count, major, minor, driver;
    CUdevice device;
    CUuuid id;
    char uuid[41], bdf[32];
    CUDA(cuInit(0));
    CUDA(cuDeviceGetCount(&count));
    if (count != 1) {
        fprintf(stderr, "expected one CUDA device, observed %d\n", count);
        goto cleanup;
    }
    CUDA(cuDeviceGet(&device, 0));
    CUDA(cuDeviceGetUuid(&id, device));
    uuid_text(&id, uuid);
    CUDA(cuDeviceGetPCIBusId(bdf, sizeof bdf, device));
    CUDA(cuDeviceGetAttribute(&major, CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR, device));
    CUDA(cuDeviceGetAttribute(&minor, CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MINOR, device));
    CUDA(cuDriverGetVersion(&driver));
    if (strcmp(uuid, argv[1]) != 0 || strcasecmp(bdf, argv[2]) != 0 || major != 7 || minor != 0) {
        fprintf(stderr, "unexpected CUDA device: %s %s sm_%d%d\n", uuid, bdf, major, minor);
        goto cleanup;
    }

    struct v100_input input;
    struct v100_output output;
    CUfunction kernel;
    make_input(&input);
    CUDA(cuCtxCreate(&context, 0, device));
    CUDA(cuMemAlloc(&device_input, sizeof input));
    CUDA(cuMemAlloc(&device_output, sizeof output));
    CUDA(cuMemcpyHtoD(device_input, &input, sizeof input));
    CUDA(cuModuleLoad(&module, argv[4]));
    CUDA(cuModuleGetFunction(&kernel, module, "v100_known_answers"));
    void *arguments[] = {&device_input, &device_output};
    CUDA(cuLaunchKernel(kernel, 1, 1, 1, COUNT, 1, 1, 0, NULL, arguments, NULL));
    CUDA(cuCtxSynchronize());
    CUDA(cuMemcpyDtoH(&output, device_output, sizeof output));
    passed = correct(&input, &output);

cleanup:
    if (module && !checked("cuModuleUnload", cuModuleUnload(module)))
        passed = 0;
    if (device_output && !checked("cuMemFree(output)", cuMemFree(device_output)))
        passed = 0;
    if (device_input && !checked("cuMemFree(input)", cuMemFree(device_input)))
        passed = 0;
    if (context && !checked("cuCtxDestroy", cuCtxDestroy(context)))
        passed = 0;
    if (passed) {
        printf("{\"uuid\":\"%s\",\"pci\":\"%s\",\"sm\":70,\"driver_api\":%d,"
               "\"devices\":1,\"libcuda\":\"/run/unifier-cuda/libcuda.so.1\","
               "\"device_bytes\":%zu,\"integer\":%d,\"fp32\":%d,\"fp64\":%d,"
               "\"fp16_input_fp32_matrix\":%d,\"context_destroyed\":true}\n",
               uuid, bdf, driver, sizeof input + sizeof output, COUNT, COUNT, COUNT, COUNT);
    }
    return passed ? 0 : 1;
}
