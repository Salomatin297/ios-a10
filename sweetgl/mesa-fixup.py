#!/usr/bin/env python3
# Mesa 26.2.4 fixes for sweetgl (Mali-G71 / panfrost on iOS, no DRM).
#
#   python3 mesa-fixup.py MESA_DIR
#
# There is no DRM on iOS: sweet_drm_pan.c stands in for the panfrost DRM
# driver over the SweetMali IOKit client. These are the places where Mesa
# would call the kernel directly:
#
#   pan_kmod.h     bo mmap -> the SweetMali mapping (made at bo creation)
#   pan_kmod.c     bo import size: lseek on a prime fd -> the shim's size
#   pan_bo.c       never munmap a SweetMali mapping (the kernel keeps it)
#   pan_device.c   tiler heap 128 -> 32 MiB (fits the phone, still plenty)
#   panfrost_kmod.c perf thread: no sched_setscheduler()/SCHED_FIFO on Darwin
#
# Idempotent: a second run reports "already applied" and changes nothing.
import sys, pathlib

def patch(path, old, new, note, marker=None):
    p = pathlib.Path(path)
    text = p.read_text()
    if (marker or new) in text:
        print("already applied: %s (%s)" % (p, note))
        return
    n = text.count(old)
    if n != 1:
        sys.exit("mesa-fixup: %s: %d matches for %r" % (p, n, note))
    p.write_text(text.replace(old, new))
    print("patched: %s (%s)" % (p, note))

def main():
    if len(sys.argv) != 2:
        sys.exit("usage: mesa-fixup.py MESA_DIR")
    m = pathlib.Path(sys.argv[1])

    # 1. pan_kmod.h: the two shim entry points and the bo mapping.
    patch(m / "src/panfrost/lib/kmod/pan_kmod.h",
"""struct pan_kmod_bo *pan_kmod_bo_import(struct pan_kmod_dev *dev, int fd);
""",
"""struct pan_kmod_bo *pan_kmod_bo_import(struct pan_kmod_dev *dev, int fd);

/* sweetgl: no DRM on iOS, the SweetMali emulation owns mappings and sizes. */
void *sweet_drm_bo_map(int fd, uint64_t offset);
uint64_t sweet_drm_prime_size(int fd);
""",
          "shim declarations")

    patch(m / "src/panfrost/lib/kmod/pan_kmod.h",
"""   host_addr =
      os_mmap(host_addr, bo->size, prot, flags, bo->dev->fd, mmap_offset);
   if (host_addr == MAP_FAILED)
      mesa_loge("mmap(..., size=%" PRIu64 ", prot=%d, flags=0x%x) failed: %s",
                bo->size, prot, flags, strerror(errno));
""",
"""   /* sweetgl: SweetMali made this mapping when the bo was created; hand
    * back the kernel's address. pan_bo.c never munmaps it for the same
    * reason.
    */
   (void)prot;
   (void)flags;
   host_addr = sweet_drm_bo_map(bo->dev->fd, (uint64_t)mmap_offset);
   if (host_addr == MAP_FAILED)
      mesa_loge("sweet_drm_bo_map(..., size=%" PRIu64 ") failed", bo->size);
""",
          "bo mmap")

    # 2. pan_kmod.c: the prime fd is a dup of /dev/null, so lseek says 0.
    patch(m / "src/panfrost/lib/kmod/pan_kmod.c",
"""      size_t size = lseek(fd, 0, SEEK_END);
      if (size == 0 || size == (size_t)-1) {
""",
"""      /* sweetgl: the prime fd is a dup of /dev/null; the shim knows the
       * kernel bo behind it and its size.
       */
      size_t size = sweet_drm_prime_size(fd);
      if (size == 0 || size == (size_t)-1) {
""",
          "prime size")

    # 3. pan_bo.c: unmapping would leave the kernel's mapping stale.
    patch(m / "src/gallium/drivers/panfrost/pan_bo.c",
"""static void
panfrost_bo_munmap(struct panfrost_bo *bo)
{
   PAN_TRACE_FUNC(PAN_TRACE_GL_BO);

   if (!bo->ptr.cpu)
      return;

   if (os_munmap((void *)(uintptr_t)bo->ptr.cpu, panfrost_bo_size(bo))) {
      mesa_loge("munmap failed: %s", strerror(errno));
      abort();
   }

   bo->ptr.cpu = NULL;
}
""",
"""static void
panfrost_bo_munmap(struct panfrost_bo *bo)
{
   PAN_TRACE_FUNC(PAN_TRACE_GL_BO);

   if (!bo->ptr.cpu)
      return;

   /* sweetgl: this is the SweetMali kernel client's mapping
    * (createMappingInTask), not an mmap of a drm fd. Unmapping it here
    * would leave the kernel's mapping stale when the bo comes back from
    * the cache. Forget the pointer, keep the mapping.
    */
   bo->ptr.cpu = NULL;
}
""",
          "no munmap")

    # 4. pan_device.c: 128 MiB is more than the phone needs to keep around.
    patch(m / "src/gallium/drivers/panfrost/pan_device.c",
"""         dev, 128 * 1024 * 1024, PAN_BO_INVISIBLE | PAN_BO_GROWABLE, "Tiler heap");
""",
"""         dev, 32 * 1024 * 1024, PAN_BO_INVISIBLE | PAN_BO_GROWABLE, "Tiler heap");
""",
          "tiler heap 32 MiB")

    # 5. panfrost_kmod.c: Darwin has no sched_setscheduler()/SCHED_FIFO.
    patch(m / "src/panfrost/lib/kmod/panfrost_kmod.c",
"""   struct panfrost_kmod_perf_session *session = data;
   const struct sched_param rt_params = {
      /* Do not use max priority to avoid starving migration and watchdog
       * threads.
       */
      .sched_priority = sched_get_priority_max(SCHED_FIFO) - 1,
   };
   /* This is best effort. If we can't make the thread RT because we don't
    * have CAP_SYS_NICE, we keep going.
    */
   int ret = sched_setscheduler(0, SCHED_FIFO, &rt_params);
   if (ret)
      mesa_logw("Can't make the perfcnt thread RT (err=%d)", errno);
""",
"""   struct panfrost_kmod_perf_session *session = data;
   /* sweetgl: Darwin has no sched_setscheduler()/SCHED_FIFO, and this
    * thread is never reached anyway (timerfd_create() fails on iOS).
    */
   int ret = 0;
""",
          "perf thread scheduling",
          marker="sweetgl: Darwin has no sched_setscheduler")

if __name__ == "__main__":
    main()
