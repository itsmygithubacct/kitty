/* SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once

#include <errno.h>
#include <poll.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

static int64_t
kilix_dmabuf_now_ms(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) return -1;
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

/* Receive exactly one frame record and one descriptor without ever leaving
 * Kitty's render thread blocked on a capture peer. The producer accepts the
 * connection and sends from its PipeWire loop, so readiness and SCM_RIGHTS
 * delivery are separate scheduling events at high frame rates. */
static int
kilix_dmabuf_receive_bounded(int sock, void *record, size_t record_size,
                             int timeout_ms) {
    int64_t deadline = kilix_dmabuf_now_ms();
    if (deadline < 0) return -1;
    deadline += timeout_ms;
    for (;;) {
        char control[CMSG_SPACE(sizeof(int))] = {0};
        struct iovec iov = {.iov_base = record, .iov_len = record_size};
        struct msghdr message = {
            .msg_iov = &iov, .msg_iovlen = 1,
            .msg_control = control, .msg_controllen = sizeof(control),
        };
        ssize_t received = recvmsg(
            sock, &message, MSG_CMSG_CLOEXEC | MSG_DONTWAIT);
        if (received < 0 && errno == EINTR) continue;
        if (received < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            int64_t now = kilix_dmabuf_now_ms();
            if (now < 0) return -1;
            int remaining = now < deadline ? (int)(deadline - now) : 0;
            if (!remaining) { errno = ETIMEDOUT; return -1; }
            struct pollfd waiter = {.fd = sock, .events = POLLIN};
            int ready = poll(&waiter, 1, remaining);
            if (ready < 0 && errno == EINTR) continue;
            if (ready < 0) return -1;
            if (!ready) { errno = ETIMEDOUT; return -1; }
            if (waiter.revents & (POLLERR | POLLNVAL)) {
                errno = EIO; return -1;
            }
            continue;
        }
        if (received < 0) return -1;
        int fd = -1;
        struct cmsghdr *header = CMSG_FIRSTHDR(&message);
        if (header && header->cmsg_level == SOL_SOCKET &&
                header->cmsg_type == SCM_RIGHTS &&
                header->cmsg_len == CMSG_LEN(sizeof(int)))
            memcpy(&fd, CMSG_DATA(header), sizeof(fd));
        if (received == (ssize_t)record_size &&
                !(message.msg_flags & (MSG_CTRUNC | MSG_TRUNC)) && fd >= 0)
            return fd;
        if (fd >= 0) close(fd);
        errno = EPROTO;
        return -1;
    }
}
