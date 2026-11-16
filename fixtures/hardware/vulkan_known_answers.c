#define _GNU_SOURCE
#include <fcntl.h>
#include <link.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>
#include <vulkan/vulkan.h>

#include "vulkan_probe_shaders.h"

enum { WIDTH = 32, HEIGHT = 16, COUNT = 16, MAX_ALLOCATION = 16 * 1024 * 1024 };
static const VkFormat format = VK_FORMAT_R8G8B8A8_UNORM;

struct Answers {
    uint32_t integers[COUNT];
    float floats[COUNT];
};

struct Buffer {
    VkBuffer handle;
    VkDeviceMemory memory;
    void *mapped;
};

struct Probe {
    VkInstance instance;
    VkPhysicalDevice physical;
    VkDevice device;
    VkQueue queue;
    uint32_t family;
    VkDeviceSize allocated;
    struct Buffer pixels;
    struct Buffer answers;
    VkImage image;
    VkDeviceMemory image_memory;
    VkImageView view;
    VkRenderPass pass;
    VkFramebuffer framebuffer;
    VkDescriptorSetLayout set_layout;
    VkDescriptorPool descriptors;
    VkDescriptorSet set;
    VkPipelineLayout graphics_layout;
    VkPipelineLayout compute_layout;
    VkPipeline graphics;
    VkPipeline compute;
    VkCommandPool commands;
    VkFence fence;
};

static void fail(const char *message)
{
    fprintf(stderr, "%s\n", message);
    exit(2);
}

static void check(VkResult result, const char *operation)
{
    if (result != VK_SUCCESS) {
        fprintf(stderr, "%s failed with VkResult %d\n", operation, result);
        exit(2);
    }
}

#define VK(call) check((call), #call)

static int anv_loaded(struct dl_phdr_info *info, size_t size, void *output)
{
    (void)size;
    const char *base = strrchr(info->dlpi_name, '/');
    if (strcmp(base ? base + 1 : info->dlpi_name, "libvulkan_intel.so") == 0) {
        *(const char **)output = info->dlpi_name;
        return 1;
    }
    return 0;
}

static void select_device(struct Probe *probe)
{
    const char *node = "/dev/dri/renderD128";
    struct stat device_node;
    int fd = open(node, O_RDWR | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0 || fstat(fd, &device_node) != 0 || !S_ISCHR(device_node.st_mode))
        fail("guest cannot open the expected DRM render node read/write");
    close(fd);

    const VkApplicationInfo app = {
        .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .pApplicationName = "Unifier Vulkan known answers",
        .apiVersion = VK_API_VERSION_1_2,
    };
    const VkInstanceCreateInfo create = {
        .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app,
    };
    VK(vkCreateInstance(&create, NULL, &probe->instance));
    VkPhysicalDevice devices[8];
    uint32_t count = sizeof devices / sizeof devices[0];
    VK(vkEnumeratePhysicalDevices(probe->instance, &count, devices));
    unsigned int matches = 0;
    for (uint32_t i = 0; i < count; ++i) {
        VkPhysicalDeviceProperties properties;
        vkGetPhysicalDeviceProperties(devices[i], &properties);
        if (properties.vendorID != 0x8086 || properties.deviceID != 0x9a49
            || properties.deviceType != VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
            || properties.apiVersion < VK_API_VERSION_1_2)
            continue;

        VkExtensionProperties extensions[512];
        uint32_t length = sizeof extensions / sizeof extensions[0];
        VK(vkEnumerateDeviceExtensionProperties(devices[i], NULL, &length, extensions));
        int has_drm = 0, has_pci = 0;
        for (uint32_t j = 0; j < length; ++j) {
            has_drm |= strcmp(extensions[j].extensionName, VK_EXT_PHYSICAL_DEVICE_DRM_EXTENSION_NAME) == 0;
            has_pci |= strcmp(extensions[j].extensionName, VK_EXT_PCI_BUS_INFO_EXTENSION_NAME) == 0;
        }
        if (!has_drm || !has_pci)
            fail("Intel Vulkan implementation lacks the required PCI or DRM identity properties");

        VkPhysicalDevicePCIBusInfoPropertiesEXT pci = {
            .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PCI_BUS_INFO_PROPERTIES_EXT,
        };
        VkPhysicalDeviceDrmPropertiesEXT drm = {
            .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRM_PROPERTIES_EXT, .pNext = &pci,
        };
        VkPhysicalDeviceDriverProperties driver = {
            .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES, .pNext = &drm,
        };
        VkPhysicalDeviceProperties2 details = {
            .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, .pNext = &driver,
        };
        vkGetPhysicalDeviceProperties2(devices[i], &details);
        if (driver.driverID != VK_DRIVER_ID_INTEL_OPEN_SOURCE_MESA || !drm.hasRender
            || drm.renderMajor != major(device_node.st_rdev) || drm.renderMinor != minor(device_node.st_rdev)
            || pci.pciDomain != 0 || pci.pciBus != 0 || pci.pciDevice != 2 || pci.pciFunction != 0)
            continue;
        probe->physical = devices[i];
        ++matches;
        printf("vendor=%04x\ndevice=%04x\ndevice_type=integrated\npci=%04x:%02x:%02x.%u\n",
               properties.vendorID, properties.deviceID, pci.pciDomain, pci.pciBus, pci.pciDevice, pci.pciFunction);
        printf("render=%u:%u\ndriver=ANV\ndevice_name=%s\ndriver_info=%s\napi=%u.%u.%u\n",
               major(device_node.st_rdev), minor(device_node.st_rdev), properties.deviceName, driver.driverInfo,
               VK_API_VERSION_MAJOR(properties.apiVersion), VK_API_VERSION_MINOR(properties.apiVersion),
               VK_API_VERSION_PATCH(properties.apiVersion));
    }
    if (count != 1 || matches != 1)
        fail("Vulkan did not enumerate exactly the expected Intel ANV physical device");
    const char *icd = NULL;
    dl_iterate_phdr(anv_loaded, &icd);
    if (!icd)
        fail("Intel ANV shared library was not loaded");
    printf("icd=%s\n", icd);

    VkQueueFamilyProperties queues[32];
    count = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(probe->physical, &count, NULL);
    if (count > sizeof queues / sizeof queues[0])
        fail("unexpected number of Vulkan queue families");
    vkGetPhysicalDeviceQueueFamilyProperties(probe->physical, &count, queues);
    const VkQueueFlags required = VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT;
    for (probe->family = 0; probe->family < count; ++probe->family)
        if (queues[probe->family].queueCount && (queues[probe->family].queueFlags & required) == required)
            break;
    if (probe->family == count)
        fail("Intel device has no joint graphics and compute queue");
    const float priority = 0.5f;
    const VkDeviceQueueCreateInfo queue = {
        .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = probe->family,
        .queueCount = 1, .pQueuePriorities = &priority,
    };
    const VkDeviceCreateInfo logical = {
        .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1, .pQueueCreateInfos = &queue,
    };
    VK(vkCreateDevice(probe->physical, &logical, NULL, &probe->device));
    vkGetDeviceQueue(probe->device, probe->family, 0, &probe->queue);
}

static VkDeviceMemory allocate(struct Probe *probe, VkMemoryRequirements memory, VkMemoryPropertyFlags flags)
{
    VkPhysicalDeviceMemoryProperties properties;
    vkGetPhysicalDeviceMemoryProperties(probe->physical, &properties);
    if (memory.size == 0 || memory.size > MAX_ALLOCATION - probe->allocated)
        fail("Vulkan explicit allocation exceeds the probe budget");
    for (uint32_t i = 0; i < properties.memoryTypeCount; ++i) {
        if ((memory.memoryTypeBits & (1u << i)) == 0 || (properties.memoryTypes[i].propertyFlags & flags) != flags)
            continue;
        const VkMemoryAllocateInfo info = {
            .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = memory.size, .memoryTypeIndex = i,
        };
        VkDeviceMemory allocated;
        VK(vkAllocateMemory(probe->device, &info, NULL, &allocated));
        probe->allocated += memory.size;
        return allocated;
    }
    fail("required Vulkan memory type is unavailable");
    return VK_NULL_HANDLE;
}

static struct Buffer make_buffer(struct Probe *probe, VkDeviceSize bytes, VkBufferUsageFlags usage)
{
    struct Buffer buffer;
    const VkBufferCreateInfo info = {
        .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = bytes, .usage = usage,
        .sharingMode = VK_SHARING_MODE_EXCLUSIVE,
    };
    VkMemoryRequirements memory;
    VK(vkCreateBuffer(probe->device, &info, NULL, &buffer.handle));
    vkGetBufferMemoryRequirements(probe->device, buffer.handle, &memory);
    buffer.memory = allocate(probe, memory, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    VK(vkBindBufferMemory(probe->device, buffer.handle, buffer.memory, 0));
    VK(vkMapMemory(probe->device, buffer.memory, 0, bytes, 0, &buffer.mapped));
    memset(buffer.mapped, 0, bytes);
    return buffer;
}

static VkPipelineShaderStageCreateInfo shader(VkDevice device, VkShaderStageFlagBits stage,
                                             const uint32_t *words, size_t bytes)
{
    const VkShaderModuleCreateInfo module = {
        .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, .codeSize = bytes, .pCode = words,
    };
    VkPipelineShaderStageCreateInfo info = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = stage, .pName = "main",
    };
    VK(vkCreateShaderModule(device, &module, NULL, &info.module));
    return info;
}

static void create_graphics(struct Probe *probe)
{
    VkDevice device = probe->device;
    probe->pixels = make_buffer(probe, WIDTH * HEIGHT * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
    const VkImageCreateInfo image = {
        .sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .imageType = VK_IMAGE_TYPE_2D, .format = format,
        .extent = {WIDTH, HEIGHT, 1}, .mipLevels = 1, .arrayLayers = 1, .samples = VK_SAMPLE_COUNT_1_BIT,
        .tiling = VK_IMAGE_TILING_OPTIMAL, .sharingMode = VK_SHARING_MODE_EXCLUSIVE,
        .usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT,
    };
    VkMemoryRequirements memory;
    VK(vkCreateImage(device, &image, NULL, &probe->image));
    vkGetImageMemoryRequirements(device, probe->image, &memory);
    probe->image_memory = allocate(probe, memory, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    VK(vkBindImageMemory(device, probe->image, probe->image_memory, 0));
    const VkImageViewCreateInfo view = {
        .sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image = probe->image,
        .viewType = VK_IMAGE_VIEW_TYPE_2D, .format = format,
        .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1},
    };
    VK(vkCreateImageView(device, &view, NULL, &probe->view));

    const VkAttachmentDescription color = {
        .format = format, .samples = VK_SAMPLE_COUNT_1_BIT,
        .loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR, .storeOp = VK_ATTACHMENT_STORE_OP_STORE,
        .stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE, .stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE,
        .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED, .finalLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
    };
    const VkAttachmentReference reference = {0, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
    const VkSubpassDescription subpass = {
        .pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS, .colorAttachmentCount = 1, .pColorAttachments = &reference,
    };
    const VkSubpassDependency after_color = {
        .srcSubpass = 0, .dstSubpass = VK_SUBPASS_EXTERNAL,
        .srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT, .dstStageMask = VK_PIPELINE_STAGE_TRANSFER_BIT,
        .srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT, .dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT,
    };
    const VkRenderPassCreateInfo pass = {
        .sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO, .attachmentCount = 1, .pAttachments = &color,
        .subpassCount = 1, .pSubpasses = &subpass, .dependencyCount = 1, .pDependencies = &after_color,
    };
    VK(vkCreateRenderPass(device, &pass, NULL, &probe->pass));
    const VkFramebufferCreateInfo framebuffer = {
        .sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO, .renderPass = probe->pass,
        .attachmentCount = 1, .pAttachments = &probe->view, .width = WIDTH, .height = HEIGHT, .layers = 1,
    };
    VK(vkCreateFramebuffer(device, &framebuffer, NULL, &probe->framebuffer));

    const VkPipelineShaderStageCreateInfo stages[] = {
        shader(device, VK_SHADER_STAGE_VERTEX_BIT, triangle_vert, sizeof triangle_vert),
        shader(device, VK_SHADER_STAGE_FRAGMENT_BIT, triangle_frag, sizeof triangle_frag),
    };
    const VkPipelineLayoutCreateInfo layout = {.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
    VK(vkCreatePipelineLayout(device, &layout, NULL, &probe->graphics_layout));
    const VkPipelineVertexInputStateCreateInfo vertices = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO,
    };
    const VkPipelineInputAssemblyStateCreateInfo assembly = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO,
        .topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST,
    };
    const VkViewport viewport = {.width = WIDTH, .height = HEIGHT, .maxDepth = 1.0f};
    const VkRect2D scissor = {{8, 4}, {16, 8}};
    const VkPipelineViewportStateCreateInfo viewport_state = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO,
        .viewportCount = 1, .pViewports = &viewport, .scissorCount = 1, .pScissors = &scissor,
    };
    const VkPipelineRasterizationStateCreateInfo raster = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO,
        .polygonMode = VK_POLYGON_MODE_FILL, .lineWidth = 1.0f,
    };
    const VkPipelineMultisampleStateCreateInfo ms = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO, .rasterizationSamples = VK_SAMPLE_COUNT_1_BIT,
    };
    const VkPipelineColorBlendAttachmentState write_rgba = {
        .colorWriteMask = VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT |
                          VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT,
    };
    const VkPipelineColorBlendStateCreateInfo blend = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO, .attachmentCount = 1, .pAttachments = &write_rgba,
    };
    const VkGraphicsPipelineCreateInfo info = {
        .sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO, .stageCount = 2, .pStages = stages,
        .pVertexInputState = &vertices, .pInputAssemblyState = &assembly, .pViewportState = &viewport_state,
        .pRasterizationState = &raster, .pMultisampleState = &ms, .pColorBlendState = &blend,
        .layout = probe->graphics_layout, .renderPass = probe->pass,
    };
    VK(vkCreateGraphicsPipelines(device, VK_NULL_HANDLE, 1, &info, NULL, &probe->graphics));
    vkDestroyShaderModule(device, stages[0].module, NULL);
    vkDestroyShaderModule(device, stages[1].module, NULL);
}

static void create_compute(struct Probe *probe)
{
    VkDevice device = probe->device;
    probe->answers = make_buffer(probe, sizeof(struct Answers), VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
    struct Answers *data = probe->answers.mapped;
    for (int i = 0; i < COUNT; ++i) {
        data->integers[i] = UINT32_C(0xfffffff0) + (uint32_t)i;
        data->floats[i] = (float)(i - 3) / 4.0f;
    }
    const VkDescriptorSetLayoutBinding binding = {
        .binding = 0, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
        .descriptorCount = 1, .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT,
    };
    const VkDescriptorSetLayoutCreateInfo layout = {
        .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO, .bindingCount = 1, .pBindings = &binding,
    };
    VK(vkCreateDescriptorSetLayout(device, &layout, NULL, &probe->set_layout));
    const VkDescriptorPoolSize size = {VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1};
    const VkDescriptorPoolCreateInfo pool = {
        .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, .maxSets = 1, .poolSizeCount = 1, .pPoolSizes = &size,
    };
    VK(vkCreateDescriptorPool(device, &pool, NULL, &probe->descriptors));
    const VkDescriptorSetAllocateInfo set = {
        .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO, .descriptorPool = probe->descriptors,
        .descriptorSetCount = 1, .pSetLayouts = &probe->set_layout,
    };
    VK(vkAllocateDescriptorSets(device, &set, &probe->set));
    const VkDescriptorBufferInfo buffer = {probe->answers.handle, 0, sizeof(struct Answers)};
    const VkWriteDescriptorSet write = {
        .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = probe->set,
        .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .pBufferInfo = &buffer,
    };
    vkUpdateDescriptorSets(device, 1, &write, 0, NULL);
    const VkPipelineLayoutCreateInfo pipeline_layout = {
        .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, .setLayoutCount = 1, .pSetLayouts = &probe->set_layout,
    };
    VK(vkCreatePipelineLayout(device, &pipeline_layout, NULL, &probe->compute_layout));
    const VkComputePipelineCreateInfo info = {
        .sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO, .layout = probe->compute_layout,
        .stage = shader(device, VK_SHADER_STAGE_COMPUTE_BIT, known_answers_comp, sizeof known_answers_comp),
    };
    VK(vkCreateComputePipelines(device, VK_NULL_HANDLE, 1, &info, NULL, &probe->compute));
    vkDestroyShaderModule(device, info.stage.module, NULL);
}

static void submit(struct Probe *probe)
{
    VkDevice device = probe->device;
    const VkCommandPoolCreateInfo pool = {
        .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, .queueFamilyIndex = probe->family,
    };
    VK(vkCreateCommandPool(device, &pool, NULL, &probe->commands));
    const VkCommandBufferAllocateInfo allocation = {
        .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, .commandPool = probe->commands,
        .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1,
    };
    VkCommandBuffer buffer;
    VK(vkAllocateCommandBuffers(device, &allocation, &buffer));
    const VkCommandBufferBeginInfo begin = {
        .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT,
    };
    VK(vkBeginCommandBuffer(buffer, &begin));
    const VkClearValue green = {.color.float32 = {0.0f, 1.0f, 0.0f, 1.0f}};
    const VkRenderPassBeginInfo pass = {
        .sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO, .renderPass = probe->pass, .framebuffer = probe->framebuffer,
        .renderArea = {{0, 0}, {WIDTH, HEIGHT}}, .clearValueCount = 1, .pClearValues = &green,
    };
    vkCmdBeginRenderPass(buffer, &pass, VK_SUBPASS_CONTENTS_INLINE);
    vkCmdBindPipeline(buffer, VK_PIPELINE_BIND_POINT_GRAPHICS, probe->graphics);
    vkCmdDraw(buffer, 3, 1, 0, 0);
    vkCmdEndRenderPass(buffer);
    const VkBufferImageCopy region = {
        .imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
        .imageExtent = {WIDTH, HEIGHT, 1},
    };
    vkCmdCopyImageToBuffer(buffer, probe->image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, probe->pixels.handle, 1, &region);
    vkCmdBindPipeline(buffer, VK_PIPELINE_BIND_POINT_COMPUTE, probe->compute);
    vkCmdBindDescriptorSets(buffer, VK_PIPELINE_BIND_POINT_COMPUTE, probe->compute_layout, 0, 1, &probe->set, 0, NULL);
    vkCmdDispatch(buffer, 1, 1, 1);
    const VkMemoryBarrier readback = {
        .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
        .srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT | VK_ACCESS_SHADER_WRITE_BIT, .dstAccessMask = VK_ACCESS_HOST_READ_BIT,
    };
    vkCmdPipelineBarrier(buffer, VK_PIPELINE_STAGE_TRANSFER_BIT | VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                         VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &readback, 0, NULL, 0, NULL);
    VK(vkEndCommandBuffer(buffer));
    const VkSubmitInfo submit_info = {
        .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &buffer,
    };
    const VkFenceCreateInfo fence = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    VK(vkCreateFence(device, &fence, NULL, &probe->fence));
    VK(vkQueueSubmit(probe->queue, 1, &submit_info, probe->fence));
    VK(vkWaitForFences(device, 1, &probe->fence, VK_TRUE, UINT64_C(3000000000)));
}

static void emit_bytes(const char *key, const void *data, size_t bytes)
{
    const unsigned char *raw = data;
    printf("%s=", key);
    for (size_t i = 0; i < bytes; ++i)
        printf("%02x", (unsigned int)raw[i]);
    putchar('\n');
}

static void destroy_buffer(VkDevice device, struct Buffer buffer)
{
    vkUnmapMemory(device, buffer.memory);
    vkDestroyBuffer(device, buffer.handle, NULL);
    vkFreeMemory(device, buffer.memory, NULL);
}

int main(void)
{
    struct Probe probe = {0};
    select_device(&probe);
    create_graphics(&probe);
    create_compute(&probe);
    submit(&probe);
    emit_bytes("rgba", probe.pixels.mapped, WIDTH * HEIGHT * 4);
    emit_bytes("compute", probe.answers.mapped, sizeof(struct Answers));
    printf("allocated_bytes=%llu\n", (unsigned long long)probe.allocated);

    VkDevice device = probe.device;
    vkDestroyFence(device, probe.fence, NULL);
    vkDestroyCommandPool(device, probe.commands, NULL);
    vkDestroyPipeline(device, probe.compute, NULL);
    vkDestroyPipeline(device, probe.graphics, NULL);
    vkDestroyPipelineLayout(device, probe.compute_layout, NULL);
    vkDestroyPipelineLayout(device, probe.graphics_layout, NULL);
    vkDestroyDescriptorPool(device, probe.descriptors, NULL);
    vkDestroyDescriptorSetLayout(device, probe.set_layout, NULL);
    vkDestroyFramebuffer(device, probe.framebuffer, NULL);
    vkDestroyRenderPass(device, probe.pass, NULL);
    vkDestroyImageView(device, probe.view, NULL);
    vkDestroyImage(device, probe.image, NULL);
    vkFreeMemory(device, probe.image_memory, NULL);
    destroy_buffer(device, probe.answers);
    destroy_buffer(device, probe.pixels);
    vkDestroyDevice(device, NULL);
    vkDestroyInstance(probe.instance, NULL);
    puts("device_destroyed=true");
    return 0;
}
