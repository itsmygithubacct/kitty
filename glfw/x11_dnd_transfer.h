// Asynchronous, bounded XDND selections. Included only by x11_window.c.
// Clipboard/PRIMARY transport is independent.
#include <X11/extensions/XRes.h>

static uint32_t
xdnd_receiver_client(Window receiver, Window requestor) {
    // Optional runtime dependency, fail closed when unavailable. No wildcard
    // resource queries, PID retrieval or global client enumeration.
    if (!receiver || !requestor || receiver > UINT32_MAX || requestor > UINT32_MAX) return 0;
    void *library = _glfw_dlopen("libXRes.so.1");
    if (!library) return 0;
    Bool (*extension)(Display*, int*, int*) = NULL;
    Status (*version)(Display*, int*, int*) = NULL;
    Status (*query)(Display*, long, XResClientIdSpec*, long*, XResClientIdValue**) = NULL;
    void (*destroy)(long, XResClientIdValue*) = NULL;
    glfw_dlsym(extension, library, "XResQueryExtension");
    glfw_dlsym(version, library, "XResQueryVersion");
    glfw_dlsym(query, library, "XResQueryClientIds");
    glfw_dlsym(destroy, library, "XResClientIdsDestroy");
    uint32_t result = 0;
    int event = 0, error = 0, major = 0, minor = 0;
    if (extension && version && query && destroy && extension(_glfw.x11.display, &event, &error) &&
            version(_glfw.x11.display, &major, &minor) &&
            (major > 1 || (major == 1 && minor >= 2))) {
        const Window named[2] = {receiver, requestor};
        const long num = receiver == requestor ? 1 : 2;
        uint32_t a = 0, b = 0;
        bool valid = true;
        for (long i = 0; valid && i < num; i++) {
            // One bounded named-resource query avoids ambiguous association
            // when the server normalizes and deduplicates same-client specs.
            XResClientIdSpec spec = {named[i], XRES_CLIENT_ID_XID_MASK};
            long count = 0;
            XResClientIdValue *ids = NULL;
            _glfwGrabErrorHandlerX11();
            Status status = query(_glfw.x11.display, 1, &spec, &count, &ids);
            _glfwReleaseErrorHandlerX11();
            valid = status == Success && _glfw.x11.errorCode == Success && count == 1 && ids;
            if (valid) {
                XResClientIdValue *v = ids;
                // A zero-length value can still have a malloc(0) pointer.
                // The client XID is the nonzero CARD32 in spec.client.
                valid = v->spec.mask == XRES_CLIENT_ID_XID_MASK && v->length == 0 &&
                    v->spec.client && v->spec.client <= UINT32_MAX;
                if (valid) {
                    if (i == 0) a = (uint32_t)v->spec.client;
                    else b = (uint32_t)v->spec.client;
                }
            }
            if (ids) destroy(count, ids);
        }
        if (receiver == requestor) b = a;
        if (valid && a && a == b) result = a;
    }
    _glfw_dlclose(library);
    return result;
}
#define XDND_MAX_BYTES (64u * 1024u * 1024u)
#define XDND_MAX_REQUESTS 8u
#define XDND_CHUNK_SIZE 32768u

static bool
xdnd_time_after(Time stamp, Time start) {
    return (uint32_t)stamp && (uint32_t)start && (uint32_t)(stamp - start) < 0x80000000u;
}

static bool
xdnd_time_in_period(Time stamp, Time start, Time end) {
    return xdnd_time_after(end, start) && xdnd_time_after(stamp, start) &&
        (uint32_t)(stamp - start) <= (uint32_t)(end - start);
}

static Bool
xdnd_is_time_event(Display *display UNUSED, XEvent *event, XPointer pointer) {
    Atom *atom = (Atom*)pointer;
    return event->type == PropertyNotify && event->xproperty.window == _glfw.x11.helperWindowHandle &&
        event->xproperty.atom == *atom && event->xproperty.state == PropertyNewValue;
}

static Time
xdnd_server_time(void) {
    // ICCCM: obtain a real timestamp with a zero-length property append.
    // The helper already selects PropertyChangeMask. Sync queues the resulting
    // event; CheckIfEvent cannot wait indefinitely for a missing notification.
    Atom atom = XInternAtom(_glfw.x11.display, "_GLFW_XDND_TIME", False);
    XEvent event;
    unsigned discarded = 0;
    while (XCheckIfEvent(_glfw.x11.display, &event, xdnd_is_time_event, (XPointer)&atom)) {
        if (++discarded == 256) return CurrentTime;
    }
    _glfwGrabErrorHandlerX11();
    XChangeProperty(_glfw.x11.display, _glfw.x11.helperWindowHandle, atom, XA_INTEGER,
        8, PropModeAppend, NULL, 0);
    _glfwReleaseErrorHandlerX11();
    if (_glfw.x11.errorCode != Success ||
            !XCheckIfEvent(_glfw.x11.display, &event, xdnd_is_time_event, (XPointer)&atom)) return CurrentTime;
    return (uint32_t)event.xproperty.time;
}

static bool
xdnd_source_generation(Window source, GLFWid instance) {
    return _glfw.x11.drag.active && _glfw.x11.drag.source_window == source && _glfw.drag.instance_id == instance;
}

static bool
xdnd_source_payload_allowed(void) {
    return _glfw.x11.drag.active && _glfw.x11.drag.dropped && _glfw.x11.drag.drop_published &&
        !_glfw.x11.drag.completing && _glfw.x11.drag.acquired && _glfw.x11.drag.accepted &&
        _glfw.x11.drag.current_target != None && _glfw.x11.drag.source_window != None &&
        xdnd_source_epoch_current();
}

static bool
xdnd_source_request_current(Window source, GLFWid instance, size_t index, uint64_t request_generation) {
    return xdnd_source_generation(source, instance) && xdnd_source_payload_allowed() &&
        index < _glfw.x11.drag.pending_count &&
        _glfw.x11.drag.pending_requests[index].generation == request_generation &&
        _glfw.x11.drag.pending_requests[index].inflight && !_glfw.x11.drag.pending_requests[index].notified;
}

static bool
xdnd_source_data_complete(void) {
    if (_glfw.x11.drag.delivery_failed || !_glfw.x11.drag.pending_count) return false;
    for (size_t i = 0; i < _glfw.x11.drag.pending_count; i++) {
        XdndDragRequest *r = &_glfw.x11.drag.pending_requests[i];
        if (!r->delivered || r->inflight) return false;
    }
    return true;
}

// Every positive wire acknowledgement is checked independently of the
// preceding property write. XSync in error-handler release can queue another
// same-XID acquisition; generation equality alone cannot authorize credit.
static bool
xdnd_selection_reply(const XdndDragRequest *r, bool success) {
    const Window source = _glfw.x11.drag.source_window;
    const GLFWid instance = _glfw.drag.instance_id;
    const bool requested_success = success;
    success = success && xdnd_source_generation(source, instance) && xdnd_source_epoch_current();
    XEvent reply = { SelectionNotify };
    reply.xselection.display = _glfw.x11.display;
    reply.xselection.requestor = r->requestor;
    reply.xselection.selection = _glfw.x11.XdndSelection;
    reply.xselection.target = r->target;
    reply.xselection.property = success ? r->property : None;
    reply.xselection.time = r->time;
    _glfwGrabErrorHandlerX11();
    XSendEvent(_glfw.x11.display, r->requestor, False, NoEventMask, &reply);
    XFlush(_glfw.x11.display);
    _glfwReleaseErrorHandlerX11();
    const bool sent = _glfw.x11.errorCode == Success;
    const bool current = success && sent && xdnd_source_generation(source, instance) &&
        xdnd_source_epoch_current();
    if ((!sent || (requested_success && !current)) && xdnd_source_generation(source, instance))
        _glfw.x11.drag.delivery_failed = true;
    return current;
}

static size_t
xdnd_retained_bytes(void) {
    size_t total = 0;
    for (size_t i = 0; i < _glfw.x11.drag.pending_count; i++) total += _glfw.x11.drag.pending_requests[i].size;
    return total;
}

static bool
xdnd_store_data(XdndDragRequest *r, const char *data, size_t size) {
    if (size > XDND_MAX_BYTES - MIN(XDND_MAX_BYTES, xdnd_retained_bytes())) return false;
    if (size + r->size > r->capacity) {
        size_t capacity = MIN(XDND_MAX_BYTES, MAX(size + r->size, MAX(r->capacity * 2, XDND_CHUNK_SIZE)));
        void *buffer = realloc(r->data, capacity);
        if (!buffer) return false;
        r->data = buffer; r->capacity = capacity;
    }
    memcpy(r->data + r->size, data, size); r->size += size;
    return true;
}

static void
xdnd_publish_data(XdndDragRequest *r) {
    const Window source = _glfw.x11.drag.source_window;
    const GLFWid instance = _glfw.drag.instance_id;
    const bool incremental = r->size > XDND_CHUNK_SIZE;
    bool success = true;
    // Resolve the fallible attributes query and event subscription first.
    // Both synchronization points may queue acquisition callbacks.
    if (incremental) {
        XWindowAttributes attributes;
        _glfwGrabErrorHandlerX11();
        success = XGetWindowAttributes(_glfw.x11.display, r->requestor, &attributes);
        _glfwReleaseErrorHandlerX11();
        success = success && _glfw.x11.errorCode == Success;
        if (success && xdnd_source_generation(source, instance) && xdnd_source_payload_allowed()) {
            _glfwGrabErrorHandlerX11();
            XSelectInput(_glfw.x11.display, r->requestor,
                attributes.your_event_mask | PropertyChangeMask | StructureNotifyMask);
            _glfwReleaseErrorHandlerX11();
            success = _glfw.x11.errorCode == Success;
        } else success = false;
    }
    success = success && xdnd_source_generation(source, instance) && xdnd_source_payload_allowed();
    if (!success) {
        r->inflight = false; r->notified = true; r->delivered = false;
        xdnd_selection_reply(r, false); return;
    }
    _glfwGrabErrorHandlerX11();
    if (incremental) {
        unsigned long size = r->size;
        XChangeProperty(_glfw.x11.display, r->requestor, r->property, _glfw.x11.INCR,
            32, PropModeReplace, (unsigned char*)&size, 1);
    } else {
        XChangeProperty(_glfw.x11.display, r->requestor, r->property, r->target,
            8, PropModeReplace, r->data, (int)r->size);
    }
    _glfwReleaseErrorHandlerX11();
    success = _glfw.x11.errorCode == Success && xdnd_source_generation(source, instance) &&
        xdnd_source_payload_allowed();
    r->incremental = incremental && success;
    r->inflight = r->incremental;
    r->notified = true;
    const bool acknowledged = xdnd_selection_reply(r, success);
    // No earlier check, or later Finished check, substitutes for this credit
    // boundary after the acknowledgement's own synchronization roundtrip.
    r->delivered = acknowledged && !incremental && xdnd_source_generation(source, instance) &&
        xdnd_source_payload_allowed();
}

static void
xdnd_poll_source_data(size_t index) {
    if (!xdnd_source_payload_allowed() || index >= _glfw.x11.drag.pending_count) return;
    Window generation = _glfw.x11.drag.source_window;
    GLFWid instance = _glfw.drag.instance_id;
    XdndDragRequest *r = &_glfw.x11.drag.pending_requests[index];
    uint64_t request_generation = r->generation;
    if (!r->inflight || r->notified || r->polling) return;
    _GLFWwindow *window = _glfwWindowForId(_glfw.drag.window_id);
    if (!window) return;
    GLFWid window_id = window->id;
    GLFWdragsourcefun callback = window->callbacks.drag_source;
    if (!callback) return;
    // A callback can free the request table, including its MIME string.
    // Retain our own name and release callback until its allocation is returned.
    char *mime = _glfw_strdup(r->mime_type);
    if (!mime) { _glfwPlatformCancelDrag(window); return; }
    r->polling = true;
    // A data-ready callback can carry several already available chunks. EOD,
    // not the first chunk, determines when SelectionNotify may be sent.
    for (unsigned count = 0; count < 32; count++) {
        GLFWDragEvent event = {.type = GLFW_DRAG_DATA_REQUEST, .mime_type = mime};
        _glfwInputDragSourceRequest(window, &event);
        bool current = xdnd_source_request_current(generation, instance, index, request_generation);
        bool chunk = event.data && event.data_sz;
        bool stored = true;
        if (current && !event.err_num && chunk) {
            r = &_glfw.x11.drag.pending_requests[index];
            stored = xdnd_store_data(r, event.data, event.data_sz);
        }
        int error = event.err_num;
        if (event.data) {
            // Return normal, error and zero-length allocations exactly once,
            // even when the fetch callback replaced the gesture. A destroyed
            // source window is represented by NULL, never a freed pointer.
            callback((GLFWwindow*)_glfwWindowForId(window_id), &event);
        }
        if (!xdnd_source_request_current(generation, instance, index, request_generation)) break;
        r = &_glfw.x11.drag.pending_requests[index];
        window = _glfwWindowForId(window_id);
        if (!window || error == EAGAIN) break;
        if (error || !stored) {
            r->inflight = false; xdnd_selection_reply(r, false);
            _glfwPlatformCancelDrag(window); break;
        }
        if (!chunk) { xdnd_publish_data(r); break; }
    }
    if (xdnd_source_generation(generation, instance) && index < _glfw.x11.drag.pending_count &&
            _glfw.x11.drag.pending_requests[index].generation == request_generation)
        _glfw.x11.drag.pending_requests[index].polling = false;
    free(mime);
}

static void
xdnd_handle_selection_request(const XSelectionRequestEvent *request) {
    const Window original_source = _glfw.x11.drag.source_window;
    const GLFWid original_instance = _glfw.drag.instance_id;
    XdndDragRequest response = {.requestor = request->requestor,
        .property = request->property ? request->property : request->target,
        .target = request->target, .time = request->time};
    // All metadata/time query dependencies precede the final lifetime check.
    const Time now = xdnd_server_time();
    const Atom delete_atom = XInternAtom(_glfw.x11.display, "DELETE", False);
    const Atom null_atom = XInternAtom(_glfw.x11.display, "NULL", False);
    const Atom timestamp_atom = XInternAtom(_glfw.x11.display, "TIMESTAMP", False);
    if (!xdnd_source_generation(original_source, original_instance) ||
            request->selection != _glfw.x11.XdndSelection || request->owner != original_source ||
            !xdnd_time_in_period(request->time, _glfw.x11.drag.acquired, now) ||
            !xdnd_source_epoch_current()) {
        xdnd_selection_reply(&response, false); return;
    }
    if (!xdnd_source_generation(original_source, original_instance)) {
        xdnd_selection_reply(&response, false); return;
    }
    if (request->target == _glfw.x11.TARGETS) {
        const size_t count = _glfw.x11.drag.type_count;
        Atom *types = malloc((count + 3) * sizeof(Atom));
        if (!types) { xdnd_selection_reply(&response, false); return; }
        memcpy(types, _glfw.x11.drag.type_atoms, count * sizeof(Atom));
        size_t n = count;
        types[n++] = _glfw.x11.TARGETS;
        types[n++] = timestamp_atom;
        if (_glfw.drag.operations & GLFW_DRAG_OPERATION_MOVE) types[n++] = delete_atom;
        if (!xdnd_source_generation(original_source, original_instance) || !xdnd_source_epoch_current()) {
            free(types); xdnd_selection_reply(&response, false); return;
        }
        _glfwGrabErrorHandlerX11();
        XChangeProperty(_glfw.x11.display, response.requestor, response.property,
            XA_ATOM, 32, PropModeReplace, (unsigned char*)types, (int)n);
        _glfwReleaseErrorHandlerX11();
        free(types);
        if (xdnd_source_generation(original_source, original_instance))
            xdnd_selection_reply(&response, _glfw.x11.errorCode == Success);
        return;
    }
    if (request->target == timestamp_atom) {
        unsigned long acquired = (uint32_t)_glfw.x11.drag.acquired;
        if (!xdnd_source_generation(original_source, original_instance) || !xdnd_source_epoch_current()) {
            xdnd_selection_reply(&response, false); return;
        }
        _glfwGrabErrorHandlerX11();
        XChangeProperty(_glfw.x11.display, response.requestor, response.property,
            XA_INTEGER, 32, PropModeReplace, (unsigned char*)&acquired, 1);
        _glfwReleaseErrorHandlerX11();
        if (xdnd_source_generation(original_source, original_instance))
            xdnd_selection_reply(&response, _glfw.x11.errorCode == Success);
        return;
    }
    // Offer metadata remains available during negotiation. No MIME conversion,
    // optional-data shortcut, data callback or INCR may precede the actual Drop.
    if (!xdnd_source_payload_allowed()) {
        xdnd_selection_reply(&response, false); return;
    }
    if (!xdnd_source_generation(original_source, original_instance) || !xdnd_source_payload_allowed()) return;
    if (request->target == delete_atom) {
        Window source = _glfw.x11.drag.source_window, receiver = _glfw.x11.drag.current_target;
        Window proxy = _glfw.x11.drag.proxy_target;
        GLFWid instance = _glfw.drag.instance_id;
        uint32_t client = xdnd_receiver_client(receiver, request->requestor);
        bool own_delivery = false;
        for (size_t i = 0; client && i < _glfw.x11.drag.pending_count; i++) {
            XdndDragRequest *r = _glfw.x11.drag.pending_requests + i;
            if (r->delivered && !r->inflight && xdnd_receiver_client(receiver, r->requestor) == client)
                own_delivery = true;
        }
        bool delivered = client && own_delivery && xdnd_source_generation(source, instance) &&
            xdnd_source_payload_allowed() && xdnd_source_data_complete() &&
            _glfw.x11.drag.current_target == receiver && _glfw.x11.drag.proxy_target == proxy &&
            _glfw.x11.drag.accepted_action == _glfw.x11.XdndActionMove &&
            xdnd_time_after(request->time, _glfw.x11.drag.drop_time);
        if (delivered) {
            _glfwGrabErrorHandlerX11();
            XChangeProperty(_glfw.x11.display, response.requestor, response.property,
                null_atom, 8, PropModeReplace, NULL, 0);
            _glfwReleaseErrorHandlerX11();
            delivered = _glfw.x11.errorCode == Success && xdnd_source_generation(source, instance) &&
                xdnd_source_payload_allowed();
        }
        delivered = xdnd_selection_reply(&response, delivered);
        if (delivered && !_glfw.x11.drag.delivery_failed && xdnd_source_generation(source, instance) &&
                xdnd_source_payload_allowed()) {
            _glfw.x11.drag.delete_receipt.valid = true;
            _glfw.x11.drag.delete_receipt.source = source;
            _glfw.x11.drag.delete_receipt.receiver = receiver;
            _glfw.x11.drag.delete_receipt.proxy = proxy;
            _glfw.x11.drag.delete_receipt.instance = instance;
            _glfw.x11.drag.delete_receipt.acquired = _glfw.x11.drag.acquired;
            _glfw.x11.drag.delete_receipt.drop_time = _glfw.x11.drag.drop_time;
            _glfw.x11.drag.delete_receipt.action = _glfw.x11.drag.accepted_action;
            _glfw.x11.drag.delete_receipt.client = client;
        }
        return;
    }
    if (!xdnd_time_after(request->time, _glfw.x11.drag.drop_time)) {
        xdnd_selection_reply(&response, false); return;
    }
    if (!xdnd_source_generation(original_source, original_instance) || !xdnd_source_payload_allowed()) {
        xdnd_selection_reply(&response, false); return;
    }
    const char *mime = NULL;
    for (size_t i = 0; i < _glfw.x11.drag.type_count; i++) {
        if (_glfw.x11.drag.type_atoms[i] == request->target) { mime = _glfw.drag.items[i].mime_type; break; }
    }
    if (!mime || _glfw.x11.drag.pending_count >= XDND_MAX_REQUESTS) {
        xdnd_selection_reply(&response, false); return;
    }
    for (size_t i = 0; i < _glfw.x11.drag.pending_count; i++) {
        XdndDragRequest *r = &_glfw.x11.drag.pending_requests[i];
        if (r->requestor == response.requestor && r->property == response.property &&
                (r->inflight || !xdnd_time_after(response.time, r->time) || (uint32_t)response.time == (uint32_t)r->time)) {
            xdnd_selection_reply(&response, false); return;
        }
    }
    if (_glfw.x11.drag.pending_count == _glfw.x11.drag.pending_capacity) {
        void *requests = realloc(_glfw.x11.drag.pending_requests, XDND_MAX_REQUESTS * sizeof(XdndDragRequest));
        if (!requests) { xdnd_selection_reply(&response, false); return; }
        _glfw.x11.drag.pending_requests = requests;
        _glfw.x11.drag.pending_capacity = XDND_MAX_REQUESTS;
    }
    size_t index = _glfw.x11.drag.pending_count++;
    XdndDragRequest *r = &_glfw.x11.drag.pending_requests[index];
    *r = response; r->mime_type = _glfw_strdup(mime); r->inflight = true;
    r->generation = ++_glfw.x11.drag.request_generation;
    if (!r->mime_type) { r->inflight = false; xdnd_selection_reply(r, false); return; }
    for (size_t i = 0; i < _glfw.drag.item_count; i++) {
        GLFWDragSourceItem *item = &_glfw.drag.items[i];
        if (strcmp(item->mime_type, mime) == 0 && item->optional_data) {
            if (xdnd_store_data(r, item->optional_data, item->data_size)) xdnd_publish_data(r);
            else { r->inflight = false; xdnd_selection_reply(r, false); }
            return;
        }
    }
    xdnd_poll_source_data(index);
}

static bool
xdnd_send_increment(XPropertyEvent *event) {
    if (event->state != PropertyDelete || !xdnd_source_payload_allowed()) return false;
    for (size_t i = 0; i < _glfw.x11.drag.pending_count; i++) {
        XdndDragRequest *r = &_glfw.x11.drag.pending_requests[i];
        if (!r->inflight || !r->incremental || r->requestor != event->window || r->property != event->atom) continue;
        if (!xdnd_time_after(event->time, r->time)) return false;
        const Window source = _glfw.x11.drag.source_window;
        const GLFWid instance = _glfw.drag.instance_id;
        size_t size = MIN(XDND_CHUNK_SIZE, r->size - r->offset);
        if (!xdnd_source_generation(source, instance) || !xdnd_source_payload_allowed()) return false;
        _glfwGrabErrorHandlerX11();
        XChangeProperty(_glfw.x11.display, r->requestor, r->property, r->target,
            8, PropModeReplace, r->data + r->offset, (int)size);
        XFlush(_glfw.x11.display);
        _glfwReleaseErrorHandlerX11();
        const bool success = _glfw.x11.errorCode == Success && xdnd_source_generation(source, instance) &&
            xdnd_source_payload_allowed();
        if (success) r->offset += size;
        if (!size || !success) {
            r->inflight = false;
            r->delivered = success;
        }
        if (!success && xdnd_source_generation(source, instance)) _glfwPlatformCancelDrag(NULL);
        return true;
    }
    return false;
}

static void
xdnd_drop_data_ready(_GLFWwindow *window, XdndSelectionRequest *r) {
    GLFWid window_id = window->id;
    uint64_t generation = _glfw.x11.xdnd.generation;
    r->inflight = false; r->got_data = true;
    const char *mimes[] = {r->mime};
    _glfwInputDropEvent(window, GLFW_DROP_DATA_AVAILABLE, 0, 0, mimes, 1, _glfw.x11.xdnd.from_self);
    if (!xdnd_receiver_current(window_id, generation)) return;
    window = _glfwWindowForId(window_id);
    // Callback may have completed/canceled the whole drop.
    for (size_t i = 0; i < _glfw.x11.xdnd.selection_requests_count; i++) {
        XdndSelectionRequest *next = &_glfw.x11.xdnd.selection_requests[i];
        if (!next->got_data) { request_selection(window, next); break; }
    }
}

static void
xdnd_read_drop_property(_GLFWwindow *window, XdndSelectionRequest *r, bool initial) {
    Atom type; int format; unsigned long count, after;
    unsigned char *data = NULL;
    const GLFWid window_id = window->id;
    const uint64_t generation = dnd.generation;
    const Atom expected = XInternAtom(_glfw.x11.display, r->mime, False);
    Window requestor = _glfw.x11.xdnd.request_window;
    if (XGetWindowProperty(_glfw.x11.display, requestor, _glfw.x11.XdndSelection,
            0, XDND_MAX_BYTES / 4 + 1, False, AnyPropertyType, &type, &format, &count, &after, &data) != Success) {
        r->error = EIO; xdnd_drop_data_ready(window, r); return;
    }
    if (type == None) { if (data) XFree(data); return; }
    bool valid = !after && xdnd_receiver_current(window_id, generation) && xdnd_receiver_epoch_current();
    if (initial && type == _glfw.x11.INCR) {
        valid = valid && format == 32 && count == 1 && data && *(unsigned long*)data <= XDND_MAX_BYTES;
        if (valid) r->announced = (size_t)*(unsigned long*)data;
        if (data) XFree(data);
        if (valid) {
            r->incremental = true;
            XDeleteProperty(_glfw.x11.display, requestor, _glfw.x11.XdndSelection);
            XFlush(_glfw.x11.display); return;
        }
    } else {
        valid = valid && format == 8 && type == expected;
        size_t retained = 0;
        for (size_t i = 0; i < _glfw.x11.xdnd.selection_requests_count; i++) retained += _glfw.x11.xdnd.selection_requests[i].size;
        valid = valid && count <= XDND_MAX_BYTES - MIN(retained, XDND_MAX_BYTES);
        if (valid && count) {
            void *buffer = realloc(r->data, r->size + count);
            if (!buffer) valid = false;
            else { r->data = buffer; memcpy(r->data + r->size, data, count); r->size += count; }
        }
        if (data) XFree(data);
    }
    valid = valid && xdnd_receiver_current(window_id, generation) && xdnd_receiver_epoch_current();
    if (valid) {
        XDeleteProperty(_glfw.x11.display, requestor, _glfw.x11.XdndSelection);
        XFlush(_glfw.x11.display);
    }
    valid = valid && xdnd_receiver_epoch_current() &&
        (!r->incremental || count || r->size >= r->announced);
    if (!valid) { r->error = EIO; xdnd_drop_data_ready(window, r); }
    else if (!r->incremental || !count) xdnd_drop_data_ready(window, r);
}

static bool
xdnd_receive_increment(XPropertyEvent *event) {
    if (event->window != _glfw.x11.xdnd.request_window || event->atom != _glfw.x11.XdndSelection) return false;
    if (event->state == PropertyNewValue) {
        XdndSelectionRequest *r = inflight_selection_request();
        _GLFWwindow *window = _glfwWindowForId(_glfw.x11.xdnd.target_id);
        if (r && r->incremental && window) xdnd_read_drop_property(window, r, false);
    }
    return true;
}

static void
xdnd_transfer_timeout(unsigned long long timer_id, void *data UNUSED) {
    if (timer_id != _glfw.x11.xdnd.transfer_timer) return;
    _glfw.x11.xdnd.transfer_timer = 0;
    _GLFWwindow *window = _glfwWindowForId(_glfw.x11.xdnd.target_id);
    if (window && _glfw.x11.xdnd.dropped) {
        GLFWid window_id = window->id;
        uint64_t generation = _glfw.x11.xdnd.generation;
        XdndSelectionRequest *r = inflight_selection_request();
        if (r) { r->error = ETIMEDOUT; xdnd_drop_data_ready(window, r); }
        if (xdnd_receiver_current(window_id, generation) && _glfw.x11.xdnd.dropped)
            _glfwPlatformEndDrop((GLFWwindow*)_glfwWindowForId(window_id), GLFW_DRAG_OPERATION_NONE);
    } else free_dnd_data();
}
