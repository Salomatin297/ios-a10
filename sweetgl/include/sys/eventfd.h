/*
 * <sys/eventfd.h> for Darwin: panfrost_kmod.c's perf-session code includes it
 * unconditionally, but eventfd does not exist on iOS. The perf thread treats
 * every call here as "perf sessions unavailable" and carries on, so the stubs
 * only have to compile and fail.
 */
#ifndef SWEET_SYS_EVENTFD_H
#define SWEET_SYS_EVENTFD_H

#include <stdint.h>
#include <errno.h>
#include <fcntl.h>

typedef uint64_t eventfd_t;

#ifndef EFD_CLOEXEC
#define EFD_CLOEXEC  O_CLOEXEC
#endif
#ifndef EFD_NONBLOCK
#define EFD_NONBLOCK O_NONBLOCK
#endif
#ifndef EFD_SEMAPHORE
#define EFD_SEMAPHORE 1
#endif

static inline int
eventfd(unsigned int initval, int flags)
{
	(void)initval;
	(void)flags;
	errno = ENOSYS;
	return -1;
}

static inline int
eventfd_read(int fd, eventfd_t *value)
{
	(void)fd;
	(void)value;
	errno = ENOSYS;
	return -1;
}

static inline int
eventfd_write(int fd, eventfd_t value)
{
	(void)fd;
	(void)value;
	errno = ENOSYS;
	return -1;
}

#endif
