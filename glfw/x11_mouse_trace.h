/* Opt-in diagnostic instrumentation. Does not alter event dispatch policy. */
#pragma once

#if defined(_GLFW_X11) && defined(__linux__)
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

/* Per translation unit, bounded to 20000 records. The operator must create a
 * private regular file first. Never log to the measured terminal or its PTY. */
static inline int x11_mouse_trace_fd(void) {
    static int fd = -2;
    int saved_errno = errno;
    if (fd == -2) {
        const char *path = getenv("KILIX_X11_MOUSE_TRACE");
        fd = -1;
        if (path && *path) {
            struct stat st;
            fd = open(path, O_WRONLY | O_APPEND);
            if (fd >= 0) {
                if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 077)) {
                    close(fd);
                    fd = -1;
                } else (void)fcntl(fd, F_SETFD, FD_CLOEXEC);
            }
            if (fd < 0) fputs("X11 mouse trace unavailable: precreate a private regular file\n", stderr);
        }
    }
    errno = saved_errno;
    return fd;
}

static inline void x11_mouse_trace(const char *phase, const char *fmt, ...)
    __attribute__((format(printf, 2, 3)));
static inline void x11_mouse_trace(const char *phase, const char *fmt, ...) {
    static unsigned count = 0;
    int saved_errno = errno;
    int fd = x11_mouse_trace_fd();
    if (fd >= 0 && count < 20000) {
        char line[768];
        struct timespec ts;
        if (clock_gettime(CLOCK_MONOTONIC, &ts) == 0) {
            long long ns = (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
            int prefix = snprintf(line, sizeof(line), "ns=%lld pid=%ld unit=%s record=%u phase=%s ", ns, (long)getpid(), __BASE_FILE__, count, phase);
            if (prefix > 0 && (size_t)prefix < sizeof(line) - 1) {
                va_list ap;
                va_start(ap, fmt);
                int n = vsnprintf(line + prefix, sizeof(line) - (size_t)prefix, fmt, ap);
                va_end(ap);
                if (n >= 0) {
                    size_t len = (size_t)prefix + (size_t)n;
                    if (len > sizeof(line) - 2) len = sizeof(line) - 2;
                    line[len++] = '\n';
                    ssize_t written = write(fd, line, len);
                    (void)written;
                }
            }
            count++;
        }
    }
    errno = saved_errno;
}
#else
#define x11_mouse_trace_fd() (-1)
#define x11_mouse_trace(...) ((void)0)
#endif
