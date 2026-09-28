"""Exercise the production drop handler with an unresponsive Xdnd peer, without X11."""
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'x11_window.c'


def function(name):
    source = SOURCE.read_text()
    start = source.index('\n' + name + '(') + 1
    brace = source.index('{', start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return 'static void\n' + source[start:end]


class DragCleanup(unittest.TestCase):
    def test_drop_lifecycle(self):
        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#define UNUSED __attribute__((unused))
#define None 0
#define CurrentTime 0
#define GLFW_DRAG_CANCELLED 1
#define ms_to_monotonic_t(x) (x)
typedef unsigned long Time;
typedef struct { int unused; } _GLFWwindow;
typedef struct { int type; } GLFWDragEvent;
static struct {
    struct { void *display; struct {
        bool active, dropped, accepted;
        unsigned long current_target, thumbnail_window, finish_timer;
    } drag; } x11;
    struct { int window_id; } drag;
} _glfw;
static int ungrabs, hides, flushes, drops, cancels, frees, timers;
static _GLFWwindow window;
static void XUngrabPointer(void *d, int t) { ungrabs++; }
static void XUnmapWindow(void *d, unsigned long w) { hides++; }
static void XFlush(void *d) { flushes++; }
static void _glfwPlatformCancelDrag(void *w) { cancels++; _glfw.x11.drag.active = false; }
static _GLFWwindow *_glfwWindowForId(int id) { return &window; }
static void _glfwInputDragSourceRequest(void *w, GLFWDragEvent *e) { assert(e->type == GLFW_DRAG_CANCELLED); cancels++; }
static void _glfwFreeDragSourceData(void) { frees++; _glfw.x11.drag.active = false; }
static void send_xdnd_drop(unsigned long w, Time t) { assert(ungrabs == 1 && hides == 1 && flushes == 1); drops++; }
static unsigned long long glfwAddTimer(int ms, bool repeat, void (*cb)(unsigned long long, void*), void *data, void *free_data) {
    assert(ms == 5000 && !repeat); timers++; return 42;
}
'''
        harness += function('drag_finish_timeout') + '\n' + function('handle_drag_button_release')
        harness += r'''
int main(void) {
    _glfw.x11.drag.active = true;
    _glfw.x11.drag.accepted = true;
    _glfw.x11.drag.current_target = 10;
    _glfw.x11.drag.thumbnail_window = 20;
    handle_drag_button_release(1);
    assert(drops == 1 && timers == 1 && !frees && !cancels);
    assert(_glfw.x11.drag.active && _glfw.x11.drag.dropped);
    handle_drag_button_release(2);
    assert(drops == 1 && ungrabs == 1); // Duplicate release cannot send another drop.
    drag_finish_timeout(42, NULL); // Peer never responds.
    assert(cancels == 1 && !_glfw.x11.drag.active && !_glfw.x11.drag.finish_timer);
    _glfw.x11.drag.active = true;
    _glfw.x11.drag.dropped = false;
    _glfw.x11.drag.accepted = false;
    handle_drag_button_release(3);
    assert(frees == 1 && cancels == 2 && drops == 1 && ungrabs == 2 && hides == 2);
    handle_drag_button_release(4);
    assert(frees == 1 && ungrabs == 2); // Inactive release is harmless.
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'test.c'
            binary = Path(directory) / 'test'
            source.write_text(harness)
            subprocess.run(['cc', '-std=c99', str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    unittest.main()
