// License: GPL v3
// Exercise the real Wayland output callbacks without a compositor connection.
#include "../glfw/wl_monitor.c"

_GLFWlibrary _glfw;

void _glfwInputMonitor(_GLFWmonitor *monitor UNUSED, int action UNUSED, int placement UNUSED) {
    abort();  // The fixture's monitor is already registered.
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
    return 0;
}
