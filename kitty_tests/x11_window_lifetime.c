// License: GPL v3
// Run on a disposable X server; never target a user's display.
#include <X11/Xlib.h>
#include <X11/Xatom.h>
#include <stdbool.h>
#include <assert.h>
#include <stdlib.h>
#include "../glfw/x11_window_lifetime.h"

static int errors;
static int previous_handler(Display *display, XErrorEvent *event) {
    (void)display; (void)event; errors++; return 0;
}
int main(void) {
    Display *d = XOpenDisplay(NULL); assert(d);
    XSetErrorHandler(previous_handler);
    Window live = XCreateSimpleWindow(d, DefaultRootWindow(d), 0, 0, 1, 1, 0, 0, 0);
    Window dead = XCreateSimpleWindow(d, DefaultRootWindow(d), 0, 0, 1, 1, 0, 0, 0);
    XDestroyWindow(d, dead); XSync(d, False);
    Atom property = XInternAtom(d, "WINDOW_LIFETIME_TEST", False);
    Atom type; int format; unsigned long count, after; unsigned char *data = NULL;
    assert(change_window_property(d, live, property, XA_STRING, 8, PropModeReplace, (unsigned char*)"ok", 2));
    assert(get_window_property(d, live, property, 0, 4, False, XA_STRING, &type, &format, &count, &after, &data) == Success);
    assert(type == XA_STRING && format == 8 && count == 2 && !after && data[0] == 'o'); XFree(data); data = NULL;
    for (int i = 0; i < 100; i++) {
        assert(get_window_property(d, dead, property, 0, 4, False, XA_STRING, &type, &format, &count, &after, &data) == BadWindow);
        assert(!data && type == None && !format && !count && !after);
        assert(!change_window_property(d, dead, property, XA_STRING, 8, PropModeReplace, (unsigned char*)"ok", 2));
        XEvent e = {0}; e.type = ClientMessage; e.xclient.format = 32;
        assert(!send_window_event(d, dead, False, 0, &e));
        assert(send_window_event(d, live, False, 0, &e));
    }
    assert(errors == 0);
    // A different error in the protected request must reach the prior handler.
    assert(get_window_property(d, live, (Atom)0xffffffffUL, 0, 4, False, XA_STRING, &type, &format, &count, &after, &data) != Success);
    assert(errors == 1);
    // An unrelated queued BadWindow must not be swallowed by the next wrapper.
    XChangeProperty(d, dead, property, XA_STRING, 8, PropModeReplace, (unsigned char*)"x", 1);
    assert(change_window_property(d, live, property, XA_STRING, 8, PropModeReplace, (unsigned char*)"ok", 2));
    assert(errors == 2);
    assert(XSetErrorHandler(previous_handler) == previous_handler);
    XDestroyWindow(d, live); XCloseDisplay(d);
    return 0;
}
