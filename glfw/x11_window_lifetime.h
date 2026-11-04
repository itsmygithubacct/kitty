// Foreign X11 windows may disappear between an event and its handling.
// Trap only BadWindow for the exact request, preserving the caller's handler
// for unrelated errors. These synchronous helpers run on the X11 event thread
// and do not invoke application callbacks while their handler is installed.
typedef struct {
    Display *display;
    unsigned long serial;
    XErrorHandler previous;
    int error;
} WindowRequest;

static WindowRequest window_request;

static int
window_request_error(Display *display, XErrorEvent *event) {
    if (display == window_request.display && event->serial == window_request.serial && event->error_code == BadWindow) {
        window_request.error = BadWindow;
        return 0;
    }
    return window_request.previous(display, event);
}

static WindowRequest
begin_window_request(Display *display) {
    WindowRequest saved = window_request;
    window_request = (WindowRequest){ .display = display, .serial = NextRequest(display) };
    window_request.previous = XSetErrorHandler(window_request_error);
    return saved;
}

static int
end_window_request(WindowRequest saved) {
    XSync(window_request.display, False);
    const int error = window_request.error;
    XSetErrorHandler(window_request.previous);
    window_request = saved;
    return error;
}

static int
get_window_property(Display *display, Window window, Atom property, long offset, long length,
                    Bool delete, Atom requested_type, Atom *type, int *format,
                    unsigned long *count, unsigned long *after, unsigned char **data) {
    *data = NULL; *type = None; *format = 0; *count = *after = 0;
    WindowRequest saved = begin_window_request(display);
    const int status = XGetWindowProperty(display, window, property, offset, length, delete,
                                         requested_type, type, format, count, after, data);
    const int error = end_window_request(saved);
    if (error) {
        if (*data) XFree(*data);
        *data = NULL; *type = None; *format = 0; *count = *after = 0;
    }
    return error ? error : status;
}

static bool
change_window_property(Display *display, Window window, Atom property, Atom type,
                       int format, int mode, const unsigned char *data, int count) {
    WindowRequest saved = begin_window_request(display);
    XChangeProperty(display, window, property, type, format, mode, data, count);
    return end_window_request(saved) == Success;
}

static Status
send_window_event(Display *display, Window window, Bool propagate, long mask, XEvent *event) {
    WindowRequest saved = begin_window_request(display);
    const Status status = XSendEvent(display, window, propagate, mask, event);
    return end_window_request(saved) == Success ? status : 0;
}
