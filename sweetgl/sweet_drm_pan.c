/*
 * A10 sweetgl: libdrm + panfrost DRM emulation for Mesa on iOS.
 *
 * Mesa's panfrost driver (src/panfrost/lib/kmod/panfrost_kmod.c,
 * src/gallium/drivers/panfrost) reaches the kernel only through the libdrm
 * calls in include/xf86drm.h and one mmap (pan_kmod.h os_mmap, redirected by
 * mesa-fixup.py to sweet_drm_bo_map). This file answers them in user space
 * on top of the SweetMali user client (xnu patch 0122):
 *
 *   CREATE_BO            sel 10 [size, flags] -> [handle, gpu va, user va, size]
 *   GEM_CLOSE            sel 11 [handle]
 *   (IOSurface import)   sel 12 [surface id] -> as sel 10
 *   SUBMIT               sel 17 [jc, requirements] -> [sequence number]: the
 *                        kernel's job queue runs it in order, the CPU goes on;
 *                        sel 13 (synchronous) when the queue is off
 *   waits                sel 18 [seq, block] -> [done]: WAIT_BO waits for the
 *                        last job that used the bo, syncobjs and sync files
 *                        carry the sequence number of the job that signals them
 *   GET_PARAM            sel 15 [param] -> [value] (panfrost param numbering)
 *   log line             sel 16 (struct input)
 *
 *   MMAP_BO              offset = handle << 32; sweet_drm_bo_map() turns it
 *                        back into the kernel's write-combined user mapping
 *   GET_BO_OFFSET        the bo's GPU VA
 *
 * One device per process; the "fd" is /dev/null. Imported IOSurfaces become
 * prime fds (dups of /dev/null) that drmPrimeFDToHandle maps back.
 */
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <mach/mach_time.h>
#include <IOKit/IOKitLib.h>

#include "xf86drm.h"
#include "drm-uapi/panfrost_drm.h"
#include "sweet_drm.h"

#define SEL_BO_NEW      10
#define SEL_BO_FREE     11
#define SEL_IMPORT      12
#define SEL_SUBMIT      13
#define SEL_PARAM       15
#define SEL_LOG         16
#define SEL_SUBMIT_ASYNC 17
#define SEL_WAIT_SEQ    18

struct sbo {
	uint64_t gpu, map, size;
	uint64_t seq;                   /* last queued job that used it */
};

struct prime {
	int fd;
	uint32_t handle;
	int consumed;                   /* Mesa imported the bo (owns the handle) */
};

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static io_connect_t conn;
static int dev_fd = -1;
static struct sbo *bos;
static uint32_t nbos;
static struct prime primes[64];
static uint32_t next_syncobj = 1;
/*
 * Job queue state. async: -1 not tried yet, 1 sel 17 works, 0 synchronous
 * (old kernel, sweetmali_async=0 or SWEETGL_SYNC=1). Sequence numbers per
 * syncobj handle, and per exported sync file (fd -> seq). Lock held.
 */
static int async = -1;
static uint64_t last_seq;
static uint64_t *sync_seq;
static uint32_t nsync;
static struct { int fd; uint64_t seq; } sfiles[256];
static uint32_t sfile_next;
static uint32_t submits, failed_submits;

/*
 * Where frame time goes (logged every 2 s of activity): GPU job time as the
 * kernel measured it, wall time spent in the submit call (the job plus
 * kernel overhead; the CPU waits for it, submits are synchronous), the
 * process's CPU time (user, system: includes the panel copy in SwapEnd) and
 * the jobs by kind. Lock held.
 */
static uint64_t win_t0, win_job_us, win_call_ns, win_max_us;
static uint32_t win_n, win_frag;
static struct rusage win_ru;

static uint64_t
now_ns(void)
{
	static mach_timebase_info_data_t tb;
	if (tb.denom == 0) {
		mach_timebase_info(&tb);
	}
	return mach_absolute_time() * tb.numer / tb.denom;
}

static uint64_t
tv_us(struct timeval t)
{
	return (uint64_t)t.tv_sec * 1000000u + (uint64_t)t.tv_usec;
}

static void
win_account(uint32_t req, uint64_t job_us, uint64_t call_ns)
{
	uint64_t t = now_ns();
	if (win_t0 == 0) {
		win_t0 = t;
		getrusage(RUSAGE_SELF, &win_ru);
	}
	win_n++;
	win_frag += (req & PANFROST_JD_REQ_FS) != 0;
	win_job_us += job_us;
	win_call_ns += call_ns;
	if (job_us > win_max_us) {
		win_max_us = job_us;
	}
	uint64_t span = t - win_t0;
	if (span < 2000000000ull) {
		return;
	}
	struct rusage ru;
	getrusage(RUSAGE_SELF, &ru);
	uint64_t usr = tv_us(ru.ru_utime) - tv_us(win_ru.ru_utime);
	uint64_t sys = tv_us(ru.ru_stime) - tv_us(win_ru.ru_stime);
	uint64_t span_ms = span / 1000000u;
	sweet_drm_log("sweetgl: time %llu ms: %u jobs (%u fragment), GPU %llu ms (max job %llu ms), "
	    "in submit %llu ms, CPU user %llu ms sys %llu ms",
	    (unsigned long long)span_ms, win_n, win_frag, (unsigned long long)(win_job_us / 1000),
	    (unsigned long long)(win_max_us / 1000), (unsigned long long)(win_call_ns / 1000000u),
	    (unsigned long long)(usr / 1000), (unsigned long long)(sys / 1000));
	win_t0 = 0;
	win_n = win_frag = 0;
	win_job_us = win_call_ns = win_max_us = 0;
}

static void connect_dev(void);

void
sweet_drm_log(const char *fmt, ...)
{
	char line[240];
	va_list ap;
	va_start(ap, fmt);
	int n = vsnprintf(line, sizeof(line), fmt, ap);
	va_end(ap);
	if (n < 0) {
		return;
	}
	if (n >= (int)sizeof(line)) {
		n = sizeof(line) - 1;
	}
	io_connect_t c = conn;
	if (!c) {
		/* early messages (library constructor): open SweetMali now */
		pthread_mutex_lock(&lock);
		if (!conn) {
			connect_dev();
		}
		pthread_mutex_unlock(&lock);
		c = conn;
	}
	if (c) {
		(void)IOConnectCallStructMethod(c, SEL_LOG, line, (size_t)n + 1, NULL, NULL);
	}
}

/* Lock held. The GPU service comes up during boot: callers may retry. */
static void
connect_dev(void)
{
	io_service_t svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("SweetMali"));
	if (svc == 0) {
		return;
	}
	kern_return_t kr = IOServiceOpen(svc, mach_task_self(), 0, &conn);
	IOObjectRelease(svc);
	if (kr != KERN_SUCCESS) {
		conn = 0;
		return;
	}
	uint64_t in = DRM_PANFROST_PARAM_GPU_PROD_ID, out = 0;
	uint32_t n = 1;
	if (IOConnectCallScalarMethod(conn, SEL_PARAM, &in, 1, &out, &n) != KERN_SUCCESS) {
		IOServiceClose(conn);
		conn = 0;
		return;
	}
	dev_fd = open("/dev/null", O_RDWR | O_CLOEXEC);
	sweet_drm_log("sweetgl: SweetMali open, GPU product 0x%llx", (unsigned long long)out);
}

int
sweet_drm_open(void)
{
	pthread_mutex_lock(&lock);
	if (!conn) {
		connect_dev();
	}
	int fd = conn && dev_fd >= 0 ? dup(dev_fd) : -1;
	pthread_mutex_unlock(&lock);
	return fd;
}

/* Lock held: the bo for a handle, or NULL. */
static struct sbo *
sbo(uint32_t handle)
{
	return handle >= 1 && handle <= nbos && bos[handle - 1].size ? &bos[handle - 1] : NULL;
}

/* Lock held. */
static int
sbo_add(uint32_t handle, uint64_t gpu, uint64_t map, uint64_t size)
{
	if (handle == 0) {
		return -EINVAL;
	}
	if (handle > nbos) {
		uint32_t n = nbos ? nbos : 256;
		while (n < handle) {
			n *= 2;
		}
		struct sbo *b = realloc(bos, n * sizeof(*b));
		if (b == NULL) {
			return -ENOMEM;
		}
		memset(b + nbos, 0, (n - nbos) * sizeof(*b));
		bos = b;
		nbos = n;
	}
	bos[handle - 1] = (struct sbo){ .gpu = gpu, .map = map, .size = size };
	return 0;
}

/* Lock held. */
static void
sync_set(uint32_t h, uint64_t seq)
{
	if (h == 0) {
		return;
	}
	if (h >= nsync) {
		uint32_t n = nsync ? nsync : 256;
		while (n <= h) {
			n *= 2;
		}
		uint64_t *p = realloc(sync_seq, n * sizeof(*p));
		if (p == NULL) {
			return;
		}
		memset(p + nsync, 0, (n - nsync) * sizeof(*p));
		sync_seq = p;
		nsync = n;
	}
	sync_seq[h] = seq;
}

/* Lock held. */
static uint64_t
sync_get(uint32_t h)
{
	return h < nsync ? sync_seq[h] : 0;
}

/* Job seq done? block: wait for it. No lock held. */
static int
seq_done(uint64_t seq, int block)
{
	if (seq == 0 || async != 1) {
		return 1;
	}
	uint64_t in[2] = {seq, block ? 1u : 0u}, out = 0;
	uint32_t n = 1;
	kern_return_t kr = IOConnectCallScalarMethod(conn, SEL_WAIT_SEQ, in, 2, &out, &n);
	return kr == KERN_SUCCESS && out != 0;
}

/* Lock held. Reply of sel 10 / 12 -> recorded bo. */
static int
bo_reply(kern_return_t kr, const uint64_t out[4], uint32_t *handle)
{
	if (kr != KERN_SUCCESS) {
		return kr == kIOReturnNoMemory ? -ENOMEM : -EINVAL;
	}
	int err = sbo_add((uint32_t)out[0], out[1], out[2], out[3]);
	if (err == 0) {
		*handle = (uint32_t)out[0];
	}
	return err;
}

static int
create_bo(struct drm_panfrost_create_bo *r)
{
	uint64_t in[2] = {r->size, r->flags}, out[4] = {0};
	uint32_t n = 4;
	pthread_mutex_lock(&lock);
	kern_return_t kr = IOConnectCallScalarMethod(conn, SEL_BO_NEW, in, 2, out, &n);
	int err = bo_reply(kr, out, &r->handle);
	if (err == 0) {
		r->offset = out[1];
	}
	pthread_mutex_unlock(&lock);
	if (err) {
		sweet_drm_log("sweetgl: CREATE_BO %u bytes flags 0x%x failed: 0x%x", r->size, r->flags, kr);
	}
	return err;
}

static int
close_bo(uint32_t handle)
{
	pthread_mutex_lock(&lock);
	struct sbo *b = sbo(handle);
	int err = -EINVAL;
	if (b != NULL) {
		uint64_t in = handle;
		IOConnectCallScalarMethod(conn, SEL_BO_FREE, &in, 1, NULL, NULL);
		memset(b, 0, sizeof(*b));
		err = 0;
	}
	pthread_mutex_unlock(&lock);
	return err;
}

static int
get_param(struct drm_panfrost_get_param *p)
{
	if (p->param <= DRM_PANFROST_PARAM_NR_CORE_GROUPS) {
		uint64_t in = p->param, out = 0;
		uint32_t n = 1;
		if (IOConnectCallScalarMethod(conn, SEL_PARAM, &in, 1, &out, &n) != KERN_SUCCESS) {
			return -EINVAL;
		}
		p->value = out;
		return 0;
	}
	switch (p->param) {
	case DRM_PANFROST_PARAM_THREAD_TLS_ALLOC:       /* 0: Mesa uses max threads per core */
	case DRM_PANFROST_PARAM_AFBC_FEATURES:          /* Midgard: no AFBC feature register */
		p->value = 0;
		return 0;
	default:
		return -EINVAL;
	}
}

static int
submit_async(struct drm_panfrost_submit *s)
{
	uint64_t in[2] = {s->jc, s->requirements}, out[2] = {0, 0};
	uint32_t n = 2;
	uint64_t c0 = now_ns();
	kern_return_t kr = IOConnectCallScalarMethod(conn, SEL_SUBMIT_ASYNC, in, 2, out, &n);
	uint64_t seq = out[0];
	uint64_t c1 = now_ns();
	pthread_mutex_lock(&lock);
	if (kr != KERN_SUCCESS) {
		async = 0;
		pthread_mutex_unlock(&lock);
		sweet_drm_log("sweetgl: no job queue (0x%x), synchronous submits", kr);
		return -1;
	}
	if (async != 1) {
		async = 1;
		sweet_drm_log("sweetgl: job queue on, submits do not wait for the GPU");
	}
	last_seq = seq;
	const uint32_t *h = (const uint32_t *)(uintptr_t)s->bo_handles;
	for (uint32_t i = 0; h != NULL && i < s->bo_handle_count; i++) {
		struct sbo *b = sbo(h[i]);
		if (b != NULL) {
			b->seq = seq;
		}
	}
	sync_set(s->out_sync, seq);
	++submits;
	win_account(s->requirements, 0, c1 - c0);
	/*
	 * The kernel counts failed queued jobs (out[1]): when it grows, a job of
	 * ours (an earlier one: the queue runs behind) failed; log the recent
	 * draws and their shaders, as for a failed synchronous submit.
	 */
	static uint64_t failed_seen;
	int dump = n >= 2 && out[1] > failed_seen;
	if (n >= 2) {
		failed_seen = out[1];
	}
	pthread_mutex_unlock(&lock);
	if (dump) {
		extern void sweetgl_draw_ring_dump(void) __attribute__((weak));
		if (sweetgl_draw_ring_dump) {
			sweetgl_draw_ring_dump();
		}
	}
	return 0;
}

static int
submit(struct drm_panfrost_submit *s)
{
	if (async != 0) {
		static int checked;
		if (!checked) {
			checked = 1;
			const char *e = getenv("SWEETGL_SYNC");
			if (e != NULL && e[0] == '1') {
				async = 0;
			}
		}
		if (async != 0 && submit_async(s) == 0) {
			return 0;
		}
	}
	uint64_t in[2] = {s->jc, s->requirements}, out[2] = {0};
	uint32_t n = 2;
	uint64_t c0 = now_ns();
	kern_return_t kr = IOConnectCallScalarMethod(conn, SEL_SUBMIT, in, 2, out, &n);
	uint64_t c1 = now_ns();
	pthread_mutex_lock(&lock);
	uint32_t nr = ++submits, nf = failed_submits += kr != KERN_SUCCESS;
	win_account(s->requirements, out[1], c1 - c0);
	pthread_mutex_unlock(&lock);
	if (kr != KERN_SUCCESS) {
		extern void sweetgl_draw_ring_dump(void) __attribute__((weak));
		if (sweetgl_draw_ring_dump) {
			sweetgl_draw_ring_dump();
		}
	}
	if (kr != KERN_SUCCESS && nf <= 20) {
		sweet_drm_log("sweetgl: submit %u (jc 0x%llx req 0x%x, %u bos) failed: 0x%x status 0x%llx",
		    nr, (unsigned long long)s->jc, s->requirements, s->bo_handle_count, kr,
		    (unsigned long long)out[0]);
	}
	if (nr == 1 || nr == 10 || nr == 100 || nr == 1000 || nr % 10000 == 0) {
		sweet_drm_log("sweetgl: %u submits, %u failed; last req 0x%x took %llu us", nr, nf,
		    s->requirements, (unsigned long long)out[1]);
	}
	/* a failed job has been stopped and the GPU reset: let Mesa carry on */
	return 0;
}

static int
command(unsigned nr, void *data)
{
	switch (nr) {
	case DRM_PANFROST_SUBMIT:
		return submit(data);
	case DRM_PANFROST_WAIT_BO: {
		struct drm_panfrost_wait_bo *w = data;
		pthread_mutex_lock(&lock);
		struct sbo *b = sbo(w->handle);
		uint64_t seq = b ? b->seq : 0;
		pthread_mutex_unlock(&lock);
		/*
		 * SWEETGL_WAITBO_BLOCK=1: a "busy?" poll (timeout 0) waits for the
		 * job instead, so Mesa never takes its busy-buffer path (swap in a new
		 * bo, copy): only that path differs from synchronous submits, and
		 * queued jobs fail with DATA_INVALID (test).
		 */
		static int block_polls = -1;
		if (block_polls < 0) {
			const char *e = getenv("SWEETGL_WAITBO_BLOCK");
			block_polls = e != NULL && e[0] == '1';
		}
		return seq_done(seq, w->timeout_ns != 0 || block_polls) ? 0 : -ETIMEDOUT;
	}
	case DRM_PANFROST_CREATE_BO:
		return create_bo(data);
	case DRM_PANFROST_MMAP_BO: {
		struct drm_panfrost_mmap_bo *m = data;
		pthread_mutex_lock(&lock);
		int ok = sbo(m->handle) != NULL;
		pthread_mutex_unlock(&lock);
		m->offset = (uint64_t)m->handle << 32;
		return ok ? 0 : -EINVAL;
	}
	case DRM_PANFROST_GET_PARAM:
		return get_param(data);
	case DRM_PANFROST_GET_BO_OFFSET: {
		struct drm_panfrost_get_bo_offset *g = data;
		pthread_mutex_lock(&lock);
		struct sbo *b = sbo(g->handle);
		g->offset = b ? b->gpu : 0;
		pthread_mutex_unlock(&lock);
		return b ? 0 : -EINVAL;
	}
	case DRM_PANFROST_MADVISE: {
		struct drm_panfrost_madvise *m = data;
		m->retained = 1;                        /* never purged */
		return 0;
	}
	case DRM_PANFROST_SET_LABEL_BO:
	case DRM_PANFROST_SYNC_BO:                      /* write-combined maps: nothing to flush */
	case DRM_PANFROST_JM_CTX_DESTROY:
		return 0;
	case DRM_PANFROST_JM_CTX_CREATE: {
		struct drm_panfrost_jm_ctx_create *c = data;
		c->handle = 1;
		return 0;
	}
	case DRM_PANFROST_QUERY_BO_INFO: {
		struct drm_panfrost_query_bo_info *q = data;
		q->extra_flags = 0;
		return 0;
	}
	default:
		return -ENOSYS;
	}
}

int
drmIoctl(int fd, unsigned long request, void *arg)
{
	(void)fd;
	if (!conn) {
		errno = ENODEV;
		return -1;
	}
	unsigned nr = (unsigned)(request & 0xff);       /* _IOC number (drm.h has no DRM_IOCTL_NR here) */
	int err;
	if (nr >= DRM_COMMAND_BASE && nr < DRM_COMMAND_END) {
		err = command(nr - DRM_COMMAND_BASE, arg);
	} else if (request == DRM_IOCTL_GEM_CLOSE) {
		err = close_bo(((struct drm_gem_close *)arg)->handle);
	} else {
		err = -ENOSYS;
	}
	if (err) {
		errno = -err;
		return -1;
	}
	return 0;
}

int
drmCommandWrite(int fd, unsigned long index, void *data, unsigned long size)
{
	(void)fd;
	(void)size;
	int err = conn ? command((unsigned)index, data) : -ENODEV;
	return err;
}

int
drmCommandWriteRead(int fd, unsigned long index, void *data, unsigned long size)
{
	return drmCommandWrite(fd, index, data, size);
}

void *
sweet_drm_bo_map(int fd, uint64_t offset)
{
	(void)fd;
	pthread_mutex_lock(&lock);
	struct sbo *b = sbo((uint32_t)(offset >> 32));
	void *p = b && b->map ? (void *)(uintptr_t)b->map : MAP_FAILED;
	pthread_mutex_unlock(&lock);
	return p;
}

int
sweet_drm_import_iosurface(uint32_t surface_id, uint64_t map, uint32_t *handle, uint64_t *size)
{
	(void)map;
	if (!conn) {
		return -ENODEV;
	}
	uint64_t in = surface_id, out[4] = {0};
	uint32_t n = 4;
	pthread_mutex_lock(&lock);
	kern_return_t kr = IOConnectCallScalarMethod(conn, SEL_IMPORT, &in, 1, out, &n);
	int err = bo_reply(kr, out, handle);
	pthread_mutex_unlock(&lock);
	if (err == 0) {
		*size = out[3];
	}
	return err;
}

int
sweet_drm_handle_to_fd(uint32_t handle)
{
	int fd = -1;
	pthread_mutex_lock(&lock);
	for (unsigned i = 0; i < sizeof(primes) / sizeof(primes[0]); i++) {
		if (primes[i].fd == 0) {
			fd = dup(dev_fd);
			if (fd >= 0) {
				primes[i] = (struct prime){ .fd = fd, .handle = handle };
			}
			break;
		}
	}
	pthread_mutex_unlock(&lock);
	return fd;
}

/* Give back a kernel bo that Mesa never imported (import failed early). */
void
sweet_drm_close_handle(uint32_t handle)
{
	close_bo(handle);
}

void
sweet_drm_close_fd(int fd)
{
	uint32_t handle = 0;
	bool free_bo = false;
	pthread_mutex_lock(&lock);
	for (unsigned i = 0; i < sizeof(primes) / sizeof(primes[0]); i++) {
		if (primes[i].fd == fd && fd > 0) {
			handle = primes[i].handle;
			free_bo = !primes[i].consumed;
			primes[i] = (struct prime){0};
			close(fd);
			break;
		}
	}
	pthread_mutex_unlock(&lock);
	if (free_bo) {
		close_bo(handle);
	}
}

/* Lock held. */
static struct prime *
prime(int fd)
{
	for (unsigned i = 0; i < sizeof(primes) / sizeof(primes[0]); i++) {
		if (primes[i].fd == fd && fd > 0) {
			return &primes[i];
		}
	}
	return NULL;
}

uint64_t
sweet_drm_prime_size(int fd)
{
	pthread_mutex_lock(&lock);
	struct prime *p = prime(fd);
	struct sbo *b = p ? sbo(p->handle) : NULL;
	uint64_t size = b ? b->size : 0;
	pthread_mutex_unlock(&lock);
	return size;
}

int
drmPrimeFDToHandle(int fd, int prime_fd, uint32_t *handle)
{
	(void)fd;
	pthread_mutex_lock(&lock);
	struct prime *p = prime(prime_fd);
	if (p != NULL) {
		*handle = p->handle;
		p->consumed = 1;
	}
	pthread_mutex_unlock(&lock);
	return p ? 0 : -EINVAL;
}

int
drmPrimeHandleToFD(int fd, uint32_t handle, uint32_t flags, int *prime_fd)
{
	(void)fd;
	(void)flags;
	*prime_fd = sweet_drm_handle_to_fd(handle);
	if (*prime_fd >= 0) {
		/* exported fd: the caller owns it, never free the bo from close_fd */
		pthread_mutex_lock(&lock);
		struct prime *p = prime(*prime_fd);
		if (p != NULL) {
			p->consumed = 1;
		}
		pthread_mutex_unlock(&lock);
	}
	return *prime_fd >= 0 ? 0 : -EMFILE;
}

int
drmCloseBufferHandle(int fd, uint32_t handle)
{
	(void)fd;
	return close_bo(handle);
}

static drmVersion version = {
	/* 1.1: HEAP/NOEXEC bo flags, nothing newer (no timestamp or coherency queries) */
	.version_major = 1, .version_minor = 1,
	.name_len = 8, .name = "panfrost",
	.date_len = 8, .date = "20261114",
	.desc_len = 9, .desc = "SweetMali",
};

drmVersionPtr
drmGetVersion(int fd)
{
	(void)fd;
	return conn ? &version : NULL;
}

void
drmFreeVersion(drmVersionPtr v)
{
	(void)v;
}

int
drmGetCap(int fd, uint64_t capability, uint64_t *value)
{
	(void)fd;
	(void)capability;
	*value = 0;
	return -EINVAL;
}

/*
 * Syncobjs carry the sequence number of the job that signals them (0:
 * signalled). Waits go to the kernel (sel 18), conservatively for the
 * newest of the handles.
 */
int
drmSyncobjCreate(int fd, uint32_t flags, uint32_t *handle)
{
	(void)fd;
	(void)flags;
	pthread_mutex_lock(&lock);
	*handle = next_syncobj++;
	sync_set(*handle, 0);
	pthread_mutex_unlock(&lock);
	return 0;
}

int
drmSyncobjDestroy(int fd, uint32_t h)
{
	(void)fd;
	pthread_mutex_lock(&lock);
	sync_set(h, 0);
	pthread_mutex_unlock(&lock);
	return 0;
}
int drmSyncobjHandleToFD(int fd, uint32_t h, int *o) { (void)fd; (void)h; *o = -1; return -ENOSYS; }
int drmSyncobjFDToHandle(int fd, int o, uint32_t *h) { (void)fd; (void)o; (void)h; return -ENOSYS; }

/* A sync file from drmSyncobjExportSyncFile: its sequence number; unknown -> the newest job. */
int
drmSyncobjImportSyncFile(int fd, uint32_t h, int s)
{
	(void)fd;
	pthread_mutex_lock(&lock);
	uint64_t seq = last_seq;
	for (unsigned i = 0; i < sizeof(sfiles) / sizeof(sfiles[0]); i++) {
		if (sfiles[i].fd == s && s > 0) {
			seq = sfiles[i].seq;
			break;
		}
	}
	sync_set(h, seq);
	pthread_mutex_unlock(&lock);
	return 0;
}
/*
 * Panfrost snapshots every flush's out-fence as a sync file (pan_fence.c);
 * failing here logged "export failed" thousands of times and gave CA no
 * fences. Everything has completed already: any fd stands for a signalled
 * fence (ImportSyncFile ignores it).
 */
int
drmSyncobjExportSyncFile(int fd, uint32_t h, int *s)
{
	(void)fd;
	*s = dev_fd >= 0 ? dup(dev_fd) : -1;
	if (*s < 0) {
		return -EMFILE;
	}
	/* fds are reused once closed: forget older entries for this fd */
	pthread_mutex_lock(&lock);
	for (unsigned i = 0; i < sizeof(sfiles) / sizeof(sfiles[0]); i++) {
		if (sfiles[i].fd == *s) {
			sfiles[i].fd = 0;
		}
	}
	sfiles[sfile_next].fd = *s;
	sfiles[sfile_next].seq = sync_get(h);
	sfile_next = (sfile_next + 1) % (sizeof(sfiles) / sizeof(sfiles[0]));
	pthread_mutex_unlock(&lock);
	return 0;
}

int
drmSyncobjReset(int fd, const uint32_t *h, uint32_t n)
{
	(void)fd;
	pthread_mutex_lock(&lock);
	for (uint32_t i = 0; i < n; i++) {
		sync_set(h[i], 0);
	}
	pthread_mutex_unlock(&lock);
	return 0;
}

int
drmSyncobjSignal(int fd, const uint32_t *h, uint32_t n)
{
	return drmSyncobjReset(fd, h, n);
}

int drmSyncobjTimelineSignal(int fd, const uint32_t *h, uint64_t *p, uint32_t n) { (void)fd; (void)h; (void)p; (void)n; return 0; }
int
drmSyncobjTransfer(int fd, uint32_t d, uint64_t dp, uint32_t s, uint64_t sp, uint32_t f)
{
	(void)fd;
	(void)dp;
	(void)sp;
	(void)f;
	pthread_mutex_lock(&lock);
	sync_set(d, sync_get(s));
	pthread_mutex_unlock(&lock);
	return 0;
}

/* Lock not held: the newest sequence number among the handles. */
static uint64_t
sync_max(const uint32_t *h, uint32_t n)
{
	uint64_t seq = 0;
	pthread_mutex_lock(&lock);
	for (uint32_t i = 0; h != NULL && i < n; i++) {
		uint64_t q = sync_get(h[i]);
		if (q > seq) {
			seq = q;
		}
	}
	pthread_mutex_unlock(&lock);
	return seq;
}

int
drmSyncobjQuery2(int fd, uint32_t *h, uint64_t *p, uint32_t n, uint32_t f)
{
	(void)fd;
	(void)f;
	for (uint32_t i = 0; i < n && p; i++) {
		pthread_mutex_lock(&lock);
		uint64_t seq = sync_get(h[i]);
		pthread_mutex_unlock(&lock);
		p[i] = seq_done(seq, 0) ? UINT64_MAX : 0;
	}
	return 0;
}

int drmSyncobjQuery(int fd, uint32_t *h, uint64_t *p, uint32_t n) { return drmSyncobjQuery2(fd, h, p, n, 0); }

int
drmSyncobjWait(int fd, uint32_t *h, unsigned n, int64_t t, unsigned f, uint32_t *s)
{
	(void)fd;
	(void)f;
	if (s) {
		*s = 0;
	}
	/* t: absolute deadline (ns); 0 or past = poll. Any later deadline waits for the job. */
	return seq_done(sync_max(h, n), t > 0) ? 0 : -ETIME;
}

int
drmSyncobjTimelineWait(int fd, uint32_t *h, uint64_t *p, unsigned n, int64_t t, unsigned f, uint32_t *s)
{
	(void)p;
	return drmSyncobjWait(fd, h, n, t, f, s);
}
