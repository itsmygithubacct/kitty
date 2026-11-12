/* Test harness for the actual X11 routing functions extracted by the Python test. */
#include <assert.h>
#include <stdbool.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <X11/Xlib.h>
#include <X11/extensions/XInput2.h>

#define GLFW_CURSOR_DISABLED 1
#define GLFW_SCROLL_OFFEST_HIGHRES 1
#define GLFW_SCROLL_OFFEST_V120 2
#define GLFW_SCROLL_OFFSET_LINES 3
#define debug_input(...) ((void)0)
#define ms_to_monotonic_t(x) (x)
typedef int GLFWOffsetType;
typedef struct { int keyboard_modifiers; double x_offset, y_offset; struct { double x,y; } unscaled; GLFWOffsetType offset_type; } GLFWScrollEvent;
typedef struct _GLFWwindow _GLFWwindow;
typedef _GLFWwindow GLFWwindow;
struct _GLFWwindow {
    unsigned long long id;
    int cursorMode;
    bool rawMouseMotion;
    double virtualCursorPosX, virtualCursorPosY;
    struct { int warpCursorPosX, warpCursorPosY, lastCursorPosX, lastCursorPosY; } x11;
    struct { void (*cursorPos)(GLFWwindow*,double,double); } callbacks;
};
/* SCROLL_TYPES */
static struct {
    struct {
        _GLFWwindow *disabledCursorWindow;
        struct { unsigned num_scroll_devices; int master_pointer_id; XIScrollDevice scroll_devices[16]; } xi;
        double contentScaleX,contentScaleY;
        Display *display;
        int context;
        unsigned long root;
        struct { bool active; unsigned last_modifiers; } drag;
    } x11;
} _glfw;
static struct { unsigned long long window_id, timer_id; GLFWScrollEvent last_event; } x11_momentum_scroll_state;
static _GLFWwindow window;
static int position_count, scroll_count, timer_cancels, momentum_cancels, momentum_count;
static bool momentum_enabled;
static GLFWScrollEvent last_scroll;
static const int momentum_scroll_gesture_detection_timeout_ms = 100;
static void position_callback(GLFWwindow *w, double x, double y) { (void)w; (void)x; (void)y; position_count++; }
/* INPUT_FUNCTION */
static void x11_cancel_momentum_scroll_timer(void) { timer_cancels++; }
static void glfw_cancel_momentum_scroll(void) { momentum_cancels++; }
static bool glfw_is_momentum_scroll_enabled(void) { return momentum_enabled; }
static int translateState(int state) { return state; }
static void _glfwInputScroll(_GLFWwindow *w, const GLFWScrollEvent *event) { (void)w; last_scroll=*event; scroll_count++; }
static void glfw_handle_scroll_event_for_momentum(_GLFWwindow *w, const GLFWScrollEvent *e, bool a, bool b) { (void)w; (void)a; (void)b; last_scroll=*e; momentum_count++; }
static void x11_scroll_stop_timer_callback(unsigned long long id, void *data) { (void)id; (void)data; }
static unsigned long long glfwAddTimer(int interval, bool repeats, void (*callback)(unsigned long long,void*), void *data, void *cleanup) {
    (void)interval; (void)repeats; (void)callback; (void)data; (void)cleanup; return 1;
}
static int fixture_find_context(Display *display, Window id, int context, XPointer *out) {
    (void)display; (void)context;
    if (id != 42) return 1;
    *out=(XPointer)&window; return 0;
}
#define XFindContext fixture_find_context
static int fixture_translate(Display *d, Window src, Window dst, int x, int y, int *rx, int *ry, Window *child) {
    (void)d; (void)src; (void)dst; *rx=x; *ry=y; *child=0; return 1;
}
#define XTranslateCoordinates fixture_translate
static void handle_drag_motion(int x,int y,unsigned long time) { (void)x; (void)y; (void)time; }
/* ROUTING_FUNCTIONS */
static void route(XEvent *event) {
/* XI_DISPATCH_BODY */
}
static void raw(XEvent *event) {
/* RAW_DISPATCH_BODY */
}
static void core(XEvent *event, _GLFWwindow *window) {
/* CORE_DISPATCH_BODY */
}
static void reset(void) {
    memset(&_glfw,0,sizeof(_glfw)); memset(&window,0,sizeof(window));
    memset(&x11_momentum_scroll_state,0,sizeof(x11_momentum_scroll_state));
    position_count=scroll_count=timer_cancels=momentum_cancels=momentum_count=0;
    momentum_enabled=false; window.id=42; window.callbacks.cursorPos=position_callback;
    _glfw.x11.xi.master_pointer_id=2;
    _glfw.x11.contentScaleX=2; _glfw.x11.contentScaleY=3;
}
static void motion(int device,int x,int y) {
    XIDeviceEvent de={.deviceid=device,.sourceid=14,.event=42,.event_x=x,.event_y=y};
    XEvent ev={0}; ev.xcookie.data=&de; route(&ev);
}
static void set_position(int x,int y) {
    window.virtualCursorPosX=x; window.virtualCursorPosY=y;
    window.x11.lastCursorPosX=x; window.x11.lastCursorPosY=y;
}
static XIScrollDevice *scroll_device(int type) {
    _glfw.x11.xi.num_scroll_devices=1;
    XIScrollDevice *d=&_glfw.x11.xi.scroll_devices[0];
    d->deviceid=d->sourceid=14; d->type_detected=true; d->offset_type=type; d->num_valuators=1;
    d->valuators[0]=(XIScrollValuator){.number=2,.increment=1,.is_vertical=true};
    return d;
}
static void scroll(int device,double value) {
    unsigned char mask=4;
    XIDeviceEvent de={.deviceid=device,.sourceid=14,.event=42,.event_x=999,.event_y=999};
    de.valuators.mask=&mask; de.valuators.mask_len=1; de.valuators.values=&value;
    XEvent ev={0}; ev.xcookie.data=&de; route(&ev);
}
int main(int argc,char **argv) {
    assert(argc==2); reset();
    if (!strcmp(argv[1],"stale_slave_master")) {
        set_position(1000,700);
        motion(14,1000,700); assert(position_count==0);
        motion(2,600,500); assert(position_count==1 && window.virtualCursorPosX==600 && window.virtualCursorPosY==500);
        motion(14,600,500); assert(position_count==1);
        motion(2,800,600); assert(position_count==2 && window.virtualCursorPosX==800 && window.virtualCursorPosY==600);
        motion(14,800,600); assert(position_count==2);
        motion(2,1000,700); assert(position_count==3 && window.virtualCursorPosX==1000);
    } else if (!strcmp(argv[1],"slave_no_scroll")) {
        set_position(50,60); motion(14,900,800);
        assert(position_count==0 && window.x11.lastCursorPosX==50 && timer_cancels==0);
        XIScrollDevice *d=scroll_device(GLFW_SCROLL_OFFSET_LINES); (void)d;
        motion(14,100,200); assert(position_count==0 && momentum_cancels==0);
    } else if (!strcmp(argv[1],"smooth_scroll")) {
        set_position(50,60); scroll_device(GLFW_SCROLL_OFFSET_LINES);
        scroll(14,10); assert(scroll_count==0 && position_count==0);
        scroll(14,12); assert(scroll_count==1 && last_scroll.y_offset==-2 && position_count==0);
        motion(2,50,60); assert(scroll_count==1 && position_count==0 && momentum_cancels==0);
        scroll(2,13); assert(scroll_count==1); /* master never duplicates slave scrolling */
    } else if (!strcmp(argv[1],"highres_momentum")) {
        set_position(50,60); XIScrollDevice *d=scroll_device(GLFW_SCROLL_OFFEST_HIGHRES);
        d->is_finger_based=true; momentum_enabled=true;
        scroll(14,10); scroll(14,10.25);
        assert(momentum_count==1 && last_scroll.y_offset==-.75 && last_scroll.unscaled.y==-.25);
        int cancels=timer_cancels;
        motion(2,50,60); assert(timer_cancels==cancels && momentum_cancels==0);
        motion(2,51,61); assert(timer_cancels==cancels+1 && momentum_cancels==1);
    } else if (!strcmp(argv[1],"v120_scroll")) {
        XIScrollDevice *d=scroll_device(GLFW_SCROLL_OFFEST_V120); d->v120_offset_needs_scaling=true;
        scroll(14,10); scroll(14,10.5);
        assert(scroll_count==1 && last_scroll.y_offset==-60 && position_count==0);
    } else if (!strcmp(argv[1],"disabled_raw")) {
        set_position(50,60); window.cursorMode=GLFW_CURSOR_DISABLED; window.rawMouseMotion=true;
        _glfw.x11.disabledCursorWindow=&window;
        motion(2,500,600); assert(position_count==0 && window.x11.lastCursorPosX==50);
        unsigned char mask=3; double values[]={4,-2};
        XIRawEvent re={0}; re.valuators.mask=&mask; re.valuators.mask_len=1; re.raw_values=values;
        XEvent ev={0}; ev.xcookie.data=&re; raw(&ev);
        assert(position_count==1 && window.virtualCursorPosX==54 && window.virtualCursorPosY==58);
    } else if (!strcmp(argv[1],"disabled_relative")) {
        set_position(50,60); window.cursorMode=GLFW_CURSOR_DISABLED; _glfw.x11.disabledCursorWindow=&window;
        window.virtualCursorPosX=100; window.virtualCursorPosY=200;
        motion(14,400,500); assert(position_count==0);
        motion(2,55,67); assert(position_count==1 && window.virtualCursorPosX==105 && window.virtualCursorPosY==207);
        _glfw.x11.disabledCursorWindow=NULL; motion(2,90,90); assert(position_count==1);
    } else if (!strcmp(argv[1],"core_fallback_duplicate")) {
        XEvent ev={0}; ev.xmotion.x=300; ev.xmotion.y=200;
        core(&ev,&window); assert(position_count==1 && window.virtualCursorPosX==300);
        motion(2,300,200); assert(position_count==1);
        motion(2,310,210); assert(position_count==2);
        ev.xmotion.x=310; ev.xmotion.y=210; core(&ev,&window); assert(position_count==2);
    } else if (!strcmp(argv[1],"unknown_window_warp")) {
        XIDeviceEvent de={.deviceid=2,.sourceid=14,.event=77,.event_x=900,.event_y=800};
        XEvent ev={0}; ev.xcookie.data=&de; route(&ev); assert(position_count==0);
        window.x11.warpCursorPosX=50; window.x11.warpCursorPosY=60;
        motion(2,50,60); assert(position_count==0 && window.x11.lastCursorPosX==50);
    } else { return 2; }
    puts("ok"); return 0;
}
