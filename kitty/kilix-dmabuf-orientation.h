#pragma once

#include <stdbool.h>
#include <stdint.h>

enum {
    KILIX_DMABUF_TRANSFORM_NONE = 0,
    KILIX_DMABUF_TRANSFORM_90 = 1,
    KILIX_DMABUF_TRANSFORM_180 = 2,
    KILIX_DMABUF_TRANSFORM_270 = 3,
    KILIX_DMABUF_TRANSFORM_FLIPPED = 4,
    KILIX_DMABUF_TRANSFORM_FLIPPED_90 = 5,
    KILIX_DMABUF_TRANSFORM_FLIPPED_180 = 6,
    KILIX_DMABUF_TRANSFORM_FLIPPED_270 = 7,
};

typedef struct {
    int32_t source_x0, source_y0, source_x1, source_y1;
} KilixDmaBufBlitRect;

/* The user-visible Kitty image path presents Weston's SPA-None buffer with
   matching X/Y orientation when copied directly. Compose logical SPA
   transforms here; X11 screenshot readback has a separate Y convention and
   must not be used as the user-visible orientation oracle. */
static inline bool
kilix_dmabuf_blit_rect(uint32_t width, uint32_t height, uint32_t transform,
                       KilixDmaBufBlitRect *rect) {
    if (!rect || !width || !height) return false;
    switch (transform) {
        case KILIX_DMABUF_TRANSFORM_NONE:
            *rect = (KilixDmaBufBlitRect){0, 0, (int32_t)width,
                                          (int32_t)height}; return true;
        case KILIX_DMABUF_TRANSFORM_180:
            *rect = (KilixDmaBufBlitRect){(int32_t)width, (int32_t)height,
                                          0, 0}; return true;
        case KILIX_DMABUF_TRANSFORM_FLIPPED:
            *rect = (KilixDmaBufBlitRect){(int32_t)width, 0, 0,
                                          (int32_t)height}; return true;
        case KILIX_DMABUF_TRANSFORM_FLIPPED_180:
            *rect = (KilixDmaBufBlitRect){0, (int32_t)height,
                                          (int32_t)width, 0}; return true;
        default:
            return false;
    }
}
