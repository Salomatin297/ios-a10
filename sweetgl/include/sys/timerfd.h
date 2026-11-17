/*
 * <sys/timerfd.h> for Darwin: panfrost_kmod.c's perf-session code includes it
 * unconditionally, but timerfd does not exist on iOS. timerfd_create() fails,
 * which is exactly the "no perf sessions" path panfrost_kmod.c already has.
 *
 * struct itimerspec is a Linux type; Darwin's <time.h> has no such thing, so
 * define it here (nothing else in the build uses the name).
 */
#ifndef SWEET_SYS_TIMERFD_H
#define SWEET_SYS_TIMERFD_H

#include <time.h>
#include <errno.h>
#include <fcntl.h>

#ifndef TFD_CLOEXEC
#define TFD_CLOEXEC       O_CLOEXEC
#endif
#ifndef TFD_NONBLOCK
#define TFD_NONBLOCK      O_NONBLOCK
#endif
#ifndef TFD_TIMER_ABSTIME
#define TFD_TIMER_ABSTIME (1 << 0)
#endif
#ifndef TFD_TIMER_CANCEL_ON_SET
#define TFD_TIMER_CANCEL_ON_SET (1 << 1)
#endif

struct itimerspec {
	struct timespec it_interval;
	struct timespec it_value;
};

static inline int
timerfd_create(int clockid, int flags)
{
	(void)clockid;
	(void)flags;
	errno = ENOSYS;
	return -1;
}

static inline int
timerfd_settime(int fd, int flags, const struct itimerspec *new_value,
    struct itimerspec *old_value)
{
	(void)fd;
	(void)flags;
	(void)new_value;
	(void)old_value;
	errno = ENOSYS;
	return -1;
}

static inline int
timerfd_gettime(int fd, struct itimerspec *curr_value)
{
	(void)fd;
	(void)curr_value;
	errno = ENOSYS;
	return -1;
}

#endif
