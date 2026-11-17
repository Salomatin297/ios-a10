/* A10 sweetgl: panfrost DRM emulation over SweetMali (sweet_drm_pan.c). */
#ifndef SWEET_DRM_H
#define SWEET_DRM_H

#include <stdint.h>

/* A "DRM fd" for the panfrost winsys (dup of /dev/null), or -1 without SweetMali. */
int sweet_drm_open(void);

/* The kernel's user mapping of the bo behind a panfrost mmap offset (MAP_FAILED if none). */
void *sweet_drm_bo_map(int fd, uint64_t offset);

/* Size of the bo behind a prime fd from sweet_drm_handle_to_fd (Mesa's lseek). */
uint64_t sweet_drm_prime_size(int fd);

/* A GEM handle for an IOSurface's memory: 0 or -errno. */
int sweet_drm_import_iosurface(uint32_t surface_id, uint64_t map, uint32_t *handle, uint64_t *size);

/* A prime fd for a handle (panfrost imports by fd only); close with sweet_drm_close_fd. */
int sweet_drm_handle_to_fd(uint32_t handle);
void sweet_drm_close_fd(int fd);

/* Give back a kernel bo Mesa never imported (no prime fd was made). */
void sweet_drm_close_handle(uint32_t handle);

/* printf-style line to the kernel log (SweetMali selector 16). */
void sweet_drm_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

#endif
