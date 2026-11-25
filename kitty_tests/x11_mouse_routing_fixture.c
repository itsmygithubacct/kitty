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
#define GLFW_CURSOR_HIDDEN 2
#define GLFW_PRESS 1
#define GLFW_RELEASE 0
#define GLFW_MOUSE_BUTTON_LEFT 0
#define GLFW_MOUSE_BUTTON_RIGHT 1
#define GLFW_MOUSE_BUTTON_MIDDLE 2
#define Button6 6
#define Button7 7
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
    int mouseButtons[8];
    double virtualCursorPosX, virtualCursorPosY;
    struct { Window handle; Colormap colormap; int warpCursorPosX, warpCursorPosY, lastCursorPosX, lastCursorPosY; } x11;
    struct { void (*cursorPos)(GLFWwindow*,double,double); } callbacks;
    struct { void (*destroy)(_GLFWwindow*); } context;
    void *monitor;
};
/* SCROLL_TYPES */
static struct {
    struct {
        _GLFWwindow *disabledCursorWindow;
        /* master_pointer_id is kept so that X11_MOUSE_SOURCE can compile earlier revisions. */
        struct { unsigned num_scroll_devices, num_master_pointers; int master_pointer_id, master_pointer_ids[8]; Window pointer_window; XIScrollDevice scroll_devices[16]; } xi;
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
static int click_count, query_count, free_count;
static bool query_failure;
static XIValuatorClassInfo seed_class, second_class, master_class;
static XIAnyClassInfo *seed_classes[1], *second_classes[1], *master_classes[1];
static XIDeviceInfo seed_device, all_devices[3];
/* The server knows master 2 and slaves 14 and 15, and answers per id. A master's classes
   copy those of its last slave, sourceid included, so master 2 also has valuator 2. */
static bool two_devices;
static inline void _glfwGrabErrorHandlerX11(void) {}
static inline void _glfwReleaseErrorHandlerX11(void) {}
/* Each query uses up one request number; events default to the last request processed. */
static unsigned long next_request, event_serial;
static inline unsigned long fixture_next_request(Display *display) { (void)display; return next_request; }
static inline XIDeviceInfo *fixture_query(Display *display,int device,int *count) {
    (void)display; query_count++; next_request++;
    if (two_devices && !query_failure) {
        if (device==XIAllDevices) { *count=3; return all_devices; }
        for (int i=0; i<3; i++) if (all_devices[i].deviceid==device) { *count=1; return all_devices+i; }
        *count=0; return NULL;
    }
    *count=query_failure ? 0 : 1; return query_failure ? NULL : &seed_device;
}
#define XNextRequest fixture_next_request
static inline void fixture_free(XIDeviceInfo *info) { (void)info; free_count++; }
#define XIQueryDevice fixture_query
#define XIFreeDeviceInfo fixture_free
static void _glfwInputMouseClick(_GLFWwindow *w,int button,int action,int mods) { (void)w; (void)button; (void)action; (void)mods; click_count++; }
static void handle_drag_button_release(unsigned long time) { (void)time; }
static void updateCursorImage(_GLFWwindow *w) { (void)w; }
static void _glfwInputCursorEnter(_GLFWwindow *w,bool entered) { (void)w; (void)entered; }
static void _glfwInputWindowFocus(_GLFWwindow *w,bool focused) { (void)w; (void)focused; }
static void disableCursor(_GLFWwindow *w) { (void)w; }
static int destroyed_handles;
static void releaseMonitor(_GLFWwindow *w) { (void)w; }
static int fixture_delete_context(Display *d, Window w, int context) { (void)d; (void)context; if (w) destroyed_handles++; return 0; }
static int fixture_window_request(Display *d, Window w) { (void)d; (void)w; return 1; }
static int fixture_flush(Display *d) { (void)d; return 1; }
#define XDeleteContext fixture_delete_context
#define XUnmapWindow fixture_window_request
#define XDestroyWindow fixture_window_request
#define XFreeColormap fixture_window_request
#define XFlush fixture_flush
/* REFRESH_FUNCTIONS */
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
static void press(XEvent *event, _GLFWwindow *window) {
/* PRESS_BODY */
}
static void release(XEvent *event, _GLFWwindow *window) {
/* RELEASE_BODY */
}
static void enter(XEvent *event, _GLFWwindow *window) {
/* ENTER_BODY */
}
static void focus(XEvent *event, _GLFWwindow *window) {
/* FOCUS_BODY */
}
static void leave(XEvent *event, _GLFWwindow *window) {
    (void)event;
/* LEAVE_BODY */
}
static void destroy(_GLFWwindow *window) {
/* DESTROY_BODY */
}
static void reset(void) {
    memset(&_glfw,0,sizeof(_glfw)); memset(&window,0,sizeof(window));
    memset(&x11_momentum_scroll_state,0,sizeof(x11_momentum_scroll_state));
    position_count=scroll_count=timer_cancels=momentum_cancels=momentum_count=0;
    click_count=query_count=free_count=destroyed_handles=0; query_failure=false;
    next_request=100; event_serial=0;
    seed_class=(XIValuatorClassInfo){.type=XIValuatorClass,.sourceid=14,.number=2,.value=10};
    seed_classes[0]=(XIAnyClassInfo*)&seed_class;
    seed_device=(XIDeviceInfo){.deviceid=14,.num_classes=1,.classes=seed_classes};
    second_class=(XIValuatorClassInfo){.type=XIValuatorClass,.sourceid=15,.number=3,.value=40};
    second_classes[0]=(XIAnyClassInfo*)&second_class;
    master_class=(XIValuatorClassInfo){.type=XIValuatorClass,.sourceid=14,.number=2,.value=999};
    master_classes[0]=(XIAnyClassInfo*)&master_class;
    all_devices[0]=(XIDeviceInfo){.deviceid=2,.use=XIMasterPointer,.num_classes=1,.classes=master_classes};
    all_devices[1]=seed_device;
    all_devices[2]=(XIDeviceInfo){.deviceid=15,.num_classes=1,.classes=second_classes};
    two_devices=false;
    momentum_enabled=false; window.id=42; window.callbacks.cursorPos=position_callback;
    window.x11.handle=42;
    _glfw.x11.xi.master_pointer_id=2;
    _glfw.x11.xi.master_pointer_ids[0]=2; _glfw.x11.xi.num_master_pointers=1;
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
    XIDeviceEvent de={.serial=event_serial ? event_serial : next_request-1,.deviceid=device,.sourceid=14,.event=42,.event_x=999,.event_y=999};
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
    } else if (!strcmp(argv[1],"emulated_wheel_position")) {
        set_position(247,526); scroll_device(GLFW_SCROLL_OFFSET_LINES);
        for (unsigned button=4; button<=7; button++) {
            XEvent ev={0}; ev.xbutton.button=button; ev.xbutton.x=246; ev.xbutton.y=526;
            press(&ev,&window); release(&ev,&window);
        }
        assert(position_count==0 && scroll_count==0 && click_count==0);
        assert(window.virtualCursorPosX==247 && timer_cancels==0);
    } else if (!strcmp(argv[1],"core_wheel_fallback")) {
        XEvent ev={0}; ev.xbutton.button=Button4; ev.xbutton.x=11; ev.xbutton.y=22;
        press(&ev,&window); release(&ev,&window);
        assert(position_count==1 && scroll_count==1 && last_scroll.y_offset==1 && click_count==0);
    } else if (!strcmp(argv[1],"real_button_position")) {
        scroll_device(GLFW_SCROLL_OFFSET_LINES);
        XEvent ev={0}; ev.xbutton.button=Button1; ev.xbutton.x=30; ev.xbutton.y=40;
        press(&ev,&window); assert(position_count==1 && click_count==1 && window.virtualCursorPosX==30);
        ev.xbutton.x=31; release(&ev,&window);
        assert(position_count==2 && click_count==2 && window.virtualCursorPosX==31);
        window.cursorMode=GLFW_CURSOR_DISABLED; ev.xbutton.x=100; press(&ev,&window);
        assert(position_count==2 && click_count==3 && window.virtualCursorPosX==31);
    } else if (!strcmp(argv[1],"first_wheel_after_focus")) {
        XIScrollDevice *d=scroll_device(GLFW_SCROLL_OFFSET_LINES); d->valuators[0].initialized=true; d->valuators[0].is_vertical=false;
        resetScrollValuators(); _glfw.x11.xi.pointer_window=42;
        XEvent ev={0}; ev.xfocus.mode=NotifyNormal; focus(&ev,&window);
        assert(query_count==1 && free_count==1);
        scroll(14,11); assert(scroll_count==1 && last_scroll.x_offset==-1);
        focus(&ev,&window); assert(query_count==1); /* valid baselines are not queried again */
    } else if (!strcmp(argv[1],"first_wheel_after_entry")) {
        XIScrollDevice *d=scroll_device(GLFW_SCROLL_OFFSET_LINES); d->valuators[0].initialized=true;
        resetScrollValuators(); seed_class.value=20;
        XEvent ev={0}; ev.xcrossing.x=50; ev.xcrossing.y=60; enter(&ev,&window);
        scroll(14,21); assert(scroll_count==1 && last_scroll.y_offset==-1 && query_count==1);
    } else if (!strcmp(argv[1],"refresh_failure_and_matching")) {
        XIScrollDevice *d=scroll_device(GLFW_SCROLL_OFFSET_LINES); _glfw.x11.xi.pointer_window=42;
        XEvent ev={0}; ev.xfocus.mode=NotifyGrab; focus(&ev,&window); assert(query_count==0);
        ev.xfocus.mode=NotifyNormal; query_failure=true; focus(&ev,&window);
        assert(query_count==1 && free_count==0 && !d->valuators[0].initialized);
        query_failure=false; seed_class.sourceid=99; focus(&ev,&window); assert(!d->valuators[0].initialized);
        seed_class.sourceid=14; seed_class.value=NAN; focus(&ev,&window); assert(!d->valuators[0].initialized);
        seed_class.value=20; focus(&ev,&window); assert(d->valuators[0].initialized && d->valuators[0].value==20);
    } else if (!strcmp(argv[1],"focus_outside_then_enter")) {
        /* Alt+Tab to the window, scroll another window 20 notches, then come in. */
        XIScrollDevice *d=scroll_device(GLFW_SCROLL_OFFSET_LINES); d->valuators[0].initialized=true; d->valuators[0].value=10;
        _glfw.x11.xi.pointer_window=42;
        XEvent ev={0}; leave(&ev,&window);
        assert(_glfw.x11.xi.pointer_window==None && !d->valuators[0].initialized);
        ev.xfocus.mode=NotifyNormal; focus(&ev,&window);
        assert(query_count==0 && !d->valuators[0].initialized);
        seed_class.value=30;
        XEvent en={0}; en.xcrossing.x=50; en.xcrossing.y=60; enter(&en,&window);
        assert(query_count==1 && _glfw.x11.xi.pointer_window==42);
        scroll(14,31); assert(scroll_count==1 && last_scroll.y_offset==-1);
    } else if (!strcmp(argv[1],"entry_discards_valid_baselines")) {
        /* Startup and hotplug seed baselines wherever the pointer is. */
        XIScrollDevice *d=scroll_device(GLFW_SCROLL_OFFSET_LINES); d->valuators[0].initialized=true; d->valuators[0].value=10;
        seed_class.value=30;
        XEvent en={0}; en.xcrossing.x=50; en.xcrossing.y=60; enter(&en,&window);
        assert(query_count==1 && d->valuators[0].value==30);
        scroll(14,31); assert(scroll_count==1 && last_scroll.y_offset==-1);
    } else if (!strcmp(argv[1],"leave_after_entering_sibling")) {
        /* pointer_window is another of our windows when this LeaveNotify comes. One pointer's
           crossings never arrive in that order (LeaveNotify first); a second master pointer's
           EnterNotify can set it. The branch is defensive; this pins what it does. */
        XIScrollDevice *d=scroll_device(GLFW_SCROLL_OFFSET_LINES); d->valuators[0].initialized=true; d->valuators[0].value=10;
        _glfw.x11.xi.pointer_window=77;
        XEvent ev={0}; leave(&ev,&window);
        assert(_glfw.x11.xi.pointer_window==77 && d->valuators[0].initialized);
        scroll(14,11); assert(scroll_count==1 && last_scroll.y_offset==-1);
        _glfw.x11.xi.pointer_window=None; leave(&ev,&window); assert(!d->valuators[0].initialized);
    } else if (!strcmp(argv[1],"second_master_pointer")) {
        /* MPX: both master pointers position, in either listing order. */
        _glfw.x11.xi.master_pointer_id=8;
        _glfw.x11.xi.master_pointer_ids[1]=8; _glfw.x11.xi.num_master_pointers=2;
        set_position(10,10);
        motion(2,600,500); assert(position_count==1 && window.virtualCursorPosX==600);
        motion(8,700,400); assert(position_count==2 && window.virtualCursorPosX==700 && window.virtualCursorPosY==400);
        motion(14,1,1); motion(9,1,1); assert(position_count==2);
    } else if (!strcmp(argv[1],"entry_query_race")) {
        /* Two notches (11, 12) were generated after the EnterNotify but before the server
           answered the query (request 100), so its reply (12) already counts them. */
        scroll_device(GLFW_SCROLL_OFFSET_LINES); seed_class.value=12;
        XEvent en={0}; en.xcrossing.x=50; en.xcrossing.y=60; enter(&en,&window);
        assert(query_count==1 && next_request==101);
        event_serial=99; scroll(14,11); assert(scroll_count==0);
        scroll(14,12); assert(scroll_count==1 && last_scroll.y_offset==-1);
        event_serial=0; scroll(14,13); assert(scroll_count==2 && last_scroll.y_offset==-1);
    } else if (!strcmp(argv[1],"one_query_for_all_devices")) {
        /* Each query is a round trip and so is the XSync after it: ask once for every device,
           and take each slave's values from its own entry, not from its master's copy. */
        XIScrollDevice *a=scroll_device(GLFW_SCROLL_OFFSET_LINES);
        XIScrollDevice *b=&_glfw.x11.xi.scroll_devices[1]; _glfw.x11.xi.num_scroll_devices=2;
        *b=*a; b->deviceid=b->sourceid=15; b->valuators[0].number=3;
        two_devices=true;
        XEvent en={0}; enter(&en,&window);
        assert(query_count==1 && free_count==1);
        assert(a->valuators[0].initialized && a->valuators[0].value==10);
        assert(b->valuators[0].initialized && b->valuators[0].value==40);
        scroll(14,11); assert(scroll_count==1 && last_scroll.y_offset==-1);
    } else if (!strcmp(argv[1],"destroy_clears_pointer_window")) {
        /* After XDeleteContext the closed window's LeaveNotify is never dispatched, so
           destruction must forget it, or FocusIn would query with the pointer outside. */
        XIScrollDevice *d=scroll_device(GLFW_SCROLL_OFFSET_LINES);
        _glfw.x11.xi.pointer_window=42;
        _GLFWwindow other=window; other.x11.handle=77;
        destroy(&other); assert(destroyed_handles==1 && _glfw.x11.xi.pointer_window==42);
        destroy(&window); assert(destroyed_handles==2 && window.x11.handle==0 && _glfw.x11.xi.pointer_window==None);
        XEvent ev={0}; ev.xfocus.mode=NotifyNormal; focus(&ev,&other);
        assert(query_count==0 && !d->valuators[0].initialized);
    } else if (!strcmp(argv[1],"unknown_window_warp")) {
        XIDeviceEvent de={.deviceid=2,.sourceid=14,.event=77,.event_x=900,.event_y=800};
        XEvent ev={0}; ev.xcookie.data=&de; route(&ev); assert(position_count==0);
        window.x11.warpCursorPosX=50; window.x11.warpCursorPosY=60;
        motion(2,50,60); assert(position_count==0 && window.x11.lastCursorPosX==50);
    } else { return 2; }
    puts("ok"); return 0;
}
