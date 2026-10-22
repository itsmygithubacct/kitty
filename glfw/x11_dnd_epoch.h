// X11-only DND lifetime observer. No clipboard changes or client enumeration.
// libXfixes remains loaded through XCloseDisplay: Xlib keeps its event hooks.
static bool
xdnd_epoch_extension(void) {
    if (_glfw.x11.dnd_fixes.attempted) return _glfw.x11.dnd_fixes.available;
    _glfw.x11.dnd_fixes.attempted = true;
    _glfw.x11.dnd_fixes.handle = _glfw_dlopen("libXfixes.so.3");
    if (!_glfw.x11.dnd_fixes.handle) return false;
    Bool (*query)(Display*, int*, int*) = NULL;
    Status (*version)(Display*, int*, int*) = NULL;
    glfw_dlsym(query, _glfw.x11.dnd_fixes.handle, "XFixesQueryExtension");
    glfw_dlsym(version, _glfw.x11.dnd_fixes.handle, "XFixesQueryVersion");
    glfw_dlsym(_glfw.x11.dnd_fixes.select, _glfw.x11.dnd_fixes.handle, "XFixesSelectSelectionInput");
    int error = 0, major = 2, minor = 0;
    _glfw.x11.dnd_fixes.available = query && version && _glfw.x11.dnd_fixes.select &&
        query(_glfw.x11.display, &_glfw.x11.dnd_fixes.event_base, &error) &&
        version(_glfw.x11.display, &major, &minor) && major >= 1;
    return _glfw.x11.dnd_fixes.available;
}

static bool
xdnd_epoch_subscribe(Window watch) {
    if (!watch || !xdnd_epoch_extension()) return false;
    _glfwGrabErrorHandlerX11();
    _glfw.x11.dnd_fixes.select(_glfw.x11.display, watch, _glfw.x11.XdndSelection,
        XFixesSetSelectionOwnerNotifyMask | XFixesSelectionWindowDestroyNotifyMask |
        XFixesSelectionClientCloseNotifyMask);
    _glfwReleaseErrorHandlerX11();
    return _glfw.x11.errorCode == Success;
}

static Bool
xdnd_epoch_event(Display *display UNUSED, XEvent *event, XPointer pointer) {
    const Window watch = *(Window*)pointer;
    const XFixesSelectionNotifyEvent *epoch = (XFixesSelectionNotifyEvent*)event;
    return _glfw.x11.dnd_fixes.available &&
        event->type == _glfw.x11.dnd_fixes.event_base + XFixesSelectionNotify &&
        epoch->window == watch && epoch->selection == _glfw.x11.XdndSelection;
}

static bool
xdnd_source_epoch_current(void) {
    if (!_glfw.x11.drag.active || !_glfw.x11.drag.source_window ||
            !_glfw.x11.drag.acquired || _glfw.x11.drag.delivery_failed ||
            (_glfw.x11.drag.pointer_released && glfwGetTime() >= _glfw.x11.drag.drop_deadline) ||
            !_glfw.x11.dnd_fixes.available) return false;
    const Window watch = _glfw.x11.drag.source_window;
    const GLFWid instance = _glfw.drag.instance_id;
    XEvent event;
    for (int pass = 0; pass < 2; pass++) {
        unsigned count = 0;
        while (XCheckIfEvent(_glfw.x11.display, &event, xdnd_epoch_event, (XPointer)&watch)) {
            const XFixesSelectionNotifyEvent *epoch = (XFixesSelectionNotifyEvent*)&event;
            // This is our real SetSelectionOwner callback, observed once.
            // A later equal-XID/equal-time notification is a replacement.
            if (!_glfw.x11.drag.epoch_ready && epoch->subtype == XFixesSetSelectionOwnerNotify &&
                    epoch->owner == watch && (uint32_t)epoch->selection_timestamp == (uint32_t)_glfw.x11.drag.acquired)
                _glfw.x11.drag.epoch_ready = true;
            else _glfw.x11.drag.delivery_failed = true;
            if (++count == 256) { _glfw.x11.drag.delivery_failed = true; break; }
        }
        if (pass == 0 && XGetSelectionOwner(_glfw.x11.display, _glfw.x11.XdndSelection) != watch)
            _glfw.x11.drag.delivery_failed = true;
    }
    return _glfw.x11.drag.active && _glfw.x11.drag.source_window == watch &&
        _glfw.drag.instance_id == instance && _glfw.x11.drag.epoch_ready && !_glfw.x11.drag.delivery_failed &&
        (!_glfw.x11.drag.pointer_released || glfwGetTime() < _glfw.x11.drag.drop_deadline);
}

static bool
xdnd_receiver_observer_current(void) {
    if (dnd.from_self)
        return dnd.source && dnd.source == _glfw.x11.drag.source_window &&
            xdnd_source_epoch_current();
    // The observer is usable before TIMESTAMP establishes the baseline. It
    // only authorizes that metadata conversion, never payload/completion.
    if (!dnd.request_window || !dnd.source || dnd.epoch_failed ||
            (dnd.dropped && glfwGetTime() >= dnd.drop_deadline)) return false;
    const Window watch = dnd.request_window, owner = dnd.source;
    const uint64_t generation = dnd.generation;
    XEvent event;
    for (int pass = 0; pass < 2; pass++) {
        if (XCheckIfEvent(_glfw.x11.display, &event, xdnd_epoch_event, (XPointer)&watch)) {
            // Even an equal XID and timestamp can denote a new acquisition.
            dnd.epoch_failed = true;
            return false;
        }
        if (pass == 0 && XGetSelectionOwner(_glfw.x11.display, _glfw.x11.XdndSelection) != owner) {
            dnd.epoch_failed = true;
            return false;
        }
    }
    return generation == dnd.generation && watch == dnd.request_window && owner == dnd.source && !dnd.epoch_failed &&
        (!dnd.dropped || glfwGetTime() < dnd.drop_deadline);
}

static bool
xdnd_receiver_epoch_current(void) {
    return (dnd.from_self || (dnd.acquired && !dnd.timestamp_pending)) &&
        xdnd_receiver_observer_current();
}
