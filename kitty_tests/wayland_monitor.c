// License: GPL v3
// Exercise the real Wayland output callbacks without a compositor connection.
#include "../glfw/wl_monitor.c"
#include "../glfw/wl_init.c"

#undef CASE
#undef C
#undef P
#undef debug
#undef info
#undef is
#undef x
#undef y

_GLFWlibrary _glfw;

void _glfwInputError(int code UNUSED, const char* format UNUSED, ...) {
    // A production error is unexpected in this fixture and must fail the test.
    abort();
}

static _GLFWwindow* focused_window;
static _GLFWmonitor* removed_monitor;
static int geometry_wait_count;

void _glfwInputMonitor(_GLFWmonitor *monitor, int action UNUSED, int placement UNUSED) {
    for (int i = 0; i < _glfw.monitorCount; i++) {
        if (_glfw.monitors[i] == monitor) {
            remove_i_from_array(_glfw.monitors, i, _glfw.monitorCount);
            return;
        }
    }
    abort();
}

_GLFWwindow* _glfwWindowForId(GLFWid id) {
    return focused_window && focused_window->id == id ? focused_window : NULL;
}

void _glfwPlatformWaitEventsTimeout(monotonic_t timeout) {
    assert(timeout > 0);
    geometry_wait_count++;
    // Dispatch the production registry callback to remove the focused output.
    registryHandleGlobalRemove(NULL, NULL, removed_monitor->wl.name);
}

monotonic_t monotonic_start_time = 0;

monotonic_t monotonic_(void) {
    static monotonic_t now;
    return now += 1000;
}

static void check_scale(_GLFWmonitor *monitor, float expected) {
    float x, y;
    _glfwPlatformGetMonitorContentScale(monitor, &x, &y);
    assert(fabsf(x - expected) < 0.001f);
    assert(fabsf(y - expected) < 0.001f);
}

int main(void) {
    GLFWvidmode mode = {.width=3840, .height=2160};
    _GLFWmonitor monitor = {.modeCount=1, .modes=&mode, .wl={.scale=2}};
    _GLFWmonitor *monitors[] = {&monitor};
    _glfw.monitorCount = 1;
    _glfw.monitors = monitors;

    // Version 2 reports completion with xdg_output.done.
    xdgOutputHandleLogicalSize(&monitor, NULL, 1920, 1080);
    xdgOutputHandleDone(&monitor, NULL);
    check_scale(&monitor, 2.0f);

    // Version 3 reports completion with wl_output.done, including later updates.
    xdgOutputHandleLogicalSize(&monitor, NULL, 2560, 1440);
    outputHandleDone(&monitor, NULL);
    check_scale(&monitor, 1.5f);
    xdgOutputHandleLogicalSize(&monitor, NULL, 3840, 2160);
    outputHandleDone(&monitor, NULL);
    check_scale(&monitor, 1.0f);

    // Rotated output updates still use the physical axes in the right order.
    monitor.wl.transform = WL_OUTPUT_TRANSFORM_90;
    xdgOutputHandleLogicalSize(&monitor, NULL, 1440, 2560);
    outputHandleDone(&monitor, NULL);
    check_scale(&monitor, 1.5f);

    // Exercise the exported API when dispatch removes its focused output.
    _GLFWmonitor removed = {
        .wl={.name=11, .scale=2, .fractional_scale=9.0,
             .xdg_output=(struct zxdg_output_v1*)1}
    };
    _GLFWmonitor survivor = {
        .wl={.name=12, .x=0, .y=0, .scale=1, .fractional_scale=1.25,
             .xdg_output=(struct zxdg_output_v1*)1,
             .xdg_size_received=true, .xdg_position_received=true}
    };
    _GLFWmonitor *scale_monitors[] = {&removed, &survivor};
    _GLFWmonitor *window_monitors[] = {&removed};
    _GLFWwindow window = {.id=77, .wl={.monitors=window_monitors, .monitorsCount=1}};
    removed_monitor = &removed;
    focused_window = &window;
    geometry_wait_count = 0;
    _glfw.initialized = true;
    _glfw.wl.xdg_output_manager = (struct zxdg_output_manager_v1*)1;
    _glfw.wl.keyboardFocusId = window.id;
    _glfw.wl.lastKeyboardFocusId = 0;
    _glfw.monitors = scale_monitors;
    _glfw.monitorCount = 2;
    _glfw.windowListHead = &window;
    assert(fabs(glfwGetWaylandCurrentMonitorFractionalScale() - 1.25) < 0.001);
    assert(geometry_wait_count == 1);
    return 0;
}
