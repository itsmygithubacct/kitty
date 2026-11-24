/*
 * embed-export.c
 * Copyright (C) 2026 Kilix contributors
 *
 * Distributed under terms of the GPL3 license.
 *
 * Embedded windows: a window launched with --type=embed lives in a hidden
 * host OS window that is never rendered. Its screen is published here, each
 * main-loop render tick that it changed, into a file the embedding
 * application maps read-only. The layout is the "KEPM" cell map documented in
 * kilix-embed-pane (include/kep_map.h there); kep_cell below must stay
 * byte-identical to that module's kep_cells.h.
 */

#include "state.h"
#include "screen.h"
#include "lineops.h"
#include <sys/mman.h>
#include <unistd.h>
#include <stdatomic.h>
#include <stddef.h>

#define KEPM_MAGIC "KEPM"
#define KEPM_VERSION 1u
#define KEPM_HEADER_SIZE 256u
#define KEPM_MAX_COLS 1024u
#define KEPM_MAX_ROWS 512u
#define KEPM_COMBINING_CAPACITY (64u * 1024u)

typedef struct kep_cell {
    uint32_t ch, extra, fg, bg, ul_color, link;
    uint16_t attrs;
    uint8_t underline, reserved;
} kep_cell;
static_assert(sizeof(kep_cell) == 28, "kep_cell must match kilix-embed-pane");

enum {
    KEP_ATTR_BOLD = 1u << 0, KEP_ATTR_ITALIC = 1u << 1, KEP_ATTR_DIM = 1u << 2, KEP_ATTR_STRIKE = 1u << 3,
    KEP_ATTR_BLINK = 1u << 4, KEP_ATTR_INVISIBLE = 1u << 5, KEP_ATTR_WIDE = 1u << 6, KEP_ATTR_WIDE_TAIL = 1u << 7,
    KEP_ATTR_SELECTED = 1u << 8, KEP_ATTR_OVERLINE = 1u << 9,
};
#define KEP_COLOR_SAME_AS_FG 0x01000000u

/* kepm_header.modes */
enum {
    KEPM_MODE_BRACKETED_PASTE = 1u << 0,
    KEPM_MODE_FOCUS_TRACKING = 1u << 1,
    KEPM_MODE_ALTERNATE_SCREEN = 1u << 2,
    KEPM_MODE_APPLICATION_CURSOR = 1u << 3,
    KEPM_MODE_REVERSE_VIDEO = 1u << 4,
};

typedef struct kepm_header {
    char magic[4];
    uint32_t version, header_size, cell_size;
    uint32_t max_cols, max_rows, combining_capacity;
    uint32_t cells_offset, dirty_offset, combining_offset;
    _Atomic uint32_t seq;           /* odd while a frame is being written */
    uint32_t cols, rows;
    uint32_t modes;                 /* KEPM_MODE_* */
    uint8_t mouse_tracking_mode;    /* 0 none, 1 button, 2 motion(drag), 3 any */
    uint8_t mouse_tracking_protocol;/* 0 normal, 1 utf8, 2 sgr, 3 urxvt, 4 sgr-pixel */
    uint8_t cursor_shape, cursor_visible, cursor_blink, cursor_focused;
    uint8_t reserved0[2];
    int32_t cursor_x, cursor_y;
    uint32_t cursor_color;
    uint32_t default_fg, default_bg;
    int32_t history_rows, view_offset;
    uint32_t combining_len;
    uint32_t key_encoding_flags;
    uint32_t reserved1;             /* explicit, so frames is 8-aligned at 104 */
    uint64_t frames;                /* frames written */
    uint32_t child_pid;
    uint32_t title_len;             /* bytes of title used */
    char title[128];                /* the window's title, UTF-8, cut at a character boundary */
    uint8_t reserved[256 - 248];
} kepm_header;
static_assert(offsetof(kepm_header, title) == 120, "kepm_header layout");
static_assert(offsetof(kepm_header, frames) == 104, "kepm_header layout");
static_assert(sizeof(kepm_header) == KEPM_HEADER_SIZE, "kepm_header is 256 bytes");

static size_t
kepm_file_size(void) {
    return KEPM_HEADER_SIZE + (size_t)KEPM_MAX_COLS * KEPM_MAX_ROWS * sizeof(kep_cell) + KEPM_MAX_ROWS
        + KEPM_COMBINING_CAPACITY * sizeof(uint32_t);
}

static uint32_t
resolve_cell_color(const ColorProfile *cp, color_type c, uint32_t defval) {
    switch (c & 0xff) {
        case 1: return cp->color_table[(c >> 8) & 0xff] & 0xffffff;
        case 2: return (c >> 8) & 0xffffff;
        default: return defval;
    }
}

static uint32_t
dyn(const ColorProfile *cp, DynamicColor overridden, DynamicColor configured, uint32_t fallback) {
    DynamicColor d = colorprofile_to_color(cp, overridden, configured);
    switch (d.type) {
        case COLOR_IS_RGB: return d.rgb & 0xffffff;
        case COLOR_IS_INDEX: return cp->color_table[d.rgb & 0xff] & 0xffffff;
        default: return fallback;
    }
}

static bool
embed_color_is_set(const ColorProfile *cp, DynamicColor overridden, DynamicColor configured) {
    DynamicColor d = colorprofile_to_color(cp, overridden, configured);
    return d.type == COLOR_IS_RGB || d.type == COLOR_IS_INDEX;
}

/* Writes one frame. Returns false if the window's size exceeds the map. */
static bool
write_frame(Window *w, Screen *s) {
    kepm_header *h = (kepm_header*)w->embed.map;
    const unsigned cols = s->columns, rows = s->lines;
    if (cols > KEPM_MAX_COLS || rows > KEPM_MAX_ROWS) return false;
    kep_cell *cells = (kep_cell*)(w->embed.map + h->cells_offset);
    uint8_t *dirty = w->embed.map + h->dirty_offset;
    uint32_t *combining = (uint32_t*)(w->embed.map + h->combining_offset);
    const ColorProfile *cp = s->color_profile;
    const bool reverse_video = s->modes.mDECSCNM;
    uint32_t dfg = dyn(cp, cp->overridden.default_fg, cp->configured.default_fg, 0xdddddd);
    uint32_t dbg = dyn(cp, cp->overridden.default_bg, cp->configured.default_bg, 0x000000);
    if (reverse_video) { uint32_t t = dfg; dfg = dbg; dbg = t; }
    const bool have_hl = embed_color_is_set(cp, cp->overridden.highlight_bg, cp->configured.highlight_bg);
    const uint32_t hl_bg = have_hl ? dyn(cp, cp->overridden.highlight_bg, cp->configured.highlight_bg, dfg) : 0;
    const uint32_t hl_fg = dyn(cp, cp->overridden.highlight_fg, cp->configured.highlight_fg, dbg);

    const unsigned sel_offset = pixel_scroll_enabled(s) ? 1 : 0;
    const size_t sel_size = (size_t)(rows + sel_offset) * cols;
    uint8_t *sel = calloc(sel_size ? sel_size : 1, 1);
    if (!sel) return false;
    screen_apply_selection(s, sel, sel_size);
    kep_cell *row_buf = malloc(sizeof(kep_cell) * (cols ? cols : 1));
    if (!row_buf) { free(sel); return false; }

    atomic_fetch_add_explicit(&h->seq, 1, memory_order_acq_rel);   /* odd: writing */
    const bool size_changed = h->cols != cols || h->rows != rows;
    h->cols = cols; h->rows = rows;
    uint32_t comb_len = 1;  /* index 0 means "none" */
    RAII_ListOfChars(lc);
    for (unsigned y = 0; y < rows; y++) {
        Line *line = screen_visual_line_for_export(s, y);
        for (unsigned x = 0; x < cols; x++) {
            const CPUCell *c = line->cpu_cells + x;
            const GPUCell *g = line->gpu_cells + x;
            kep_cell *o = row_buf + x;
            memset(o, 0, sizeof *o);
            const CellAttrs a = g->attrs;
            uint32_t fg = resolve_cell_color(cp, g->fg, dfg), bg = resolve_cell_color(cp, g->bg, dbg);
            if (a.reverse) { uint32_t t = fg; fg = bg; bg = t; }
            uint16_t attrs = 0;
            if (a.bold) attrs |= KEP_ATTR_BOLD;
            if (a.italic) attrs |= KEP_ATTR_ITALIC;
            if (a.dim) attrs |= KEP_ATTR_DIM;
            if (a.strike) attrs |= KEP_ATTR_STRIKE;
            if (a.blink) attrs |= KEP_ATTR_BLINK;
            if (c->is_multicell) {
                if (c->x || c->y) attrs |= KEP_ATTR_WIDE_TAIL;
                else if (c->width > 1) attrs |= KEP_ATTR_WIDE;
            }
            const uint8_t selmask = sel[(size_t)(y + sel_offset) * cols + x];
            if (selmask & 1) {
                attrs |= KEP_ATTR_SELECTED;
                if (have_hl) { bg = hl_bg; fg = hl_fg; }
                else { uint32_t t = fg; fg = bg; bg = t; }
            }
            o->fg = fg; o->bg = bg;
            const unsigned decoration = a.decoration;
            o->underline = (selmask & 2) && !decoration ? 1 : (uint8_t)(decoration > 5u ? 5u : decoration);
            o->ul_color = (g->decoration_fg & 0xff) ? resolve_cell_color(cp, g->decoration_fg, fg) : KEP_COLOR_SAME_AS_FG;
            o->link = c->hyperlink_id;
            o->attrs = attrs;
            if (!(attrs & KEP_ATTR_WIDE_TAIL) && cell_has_text(c)) {
                text_in_cell(c, s->text_cache, &lc);
                /* A multicell's text-cache entry starts with its marker (see line.c, cell_text). */
                const size_t first = (c->is_multicell && c->ch_is_idx && lc.count) ? 1 : 0;
                const size_t n = lc.count - first;
                o->ch = n ? lc.chars[first] : 0;
                if (n > 1 && comb_len + n <= KEPM_COMBINING_CAPACITY) {
                    o->extra = comb_len;
                    combining[comb_len++] = (uint32_t)(n - 1);
                    for (size_t i = 1; i < n; i++) combining[comb_len++] = lc.chars[first + i];
                }
            }
        }
        kep_cell *dest = cells + (size_t)y * cols;
        dirty[y] = size_changed || w->embed.force || memcmp(dest, row_buf, sizeof(kep_cell) * cols) != 0;
        if (dirty[y]) memcpy(dest, row_buf, sizeof(kep_cell) * cols);
    }
    h->combining_len = comb_len;
    const Cursor *cur = s->cursor;
    h->cursor_x = (int32_t)cur->x; h->cursor_y = (int32_t)cur->y + (int32_t)s->scrolled_by;
    /* kep_cells.h numbering: block 0, beam 1, underline 2, hollow 3 (the engine's are one higher) */
    const unsigned shape = cur->shape ? cur->shape : OPT(cursor_shape);
    h->cursor_shape = (uint8_t)(shape >= CURSOR_BLOCK && shape <= CURSOR_HOLLOW ? shape - CURSOR_BLOCK : 0);
    h->cursor_visible = screen_is_cursor_visible(s) && h->cursor_y < (int32_t)rows;
    h->cursor_blink = !cur->non_blinking;
    h->cursor_focused = s->has_focus;
    h->cursor_color = embed_color_is_set(cp, cp->overridden.cursor_color, cp->configured.cursor_color)
        ? dyn(cp, cp->overridden.cursor_color, cp->configured.cursor_color, dfg) : KEP_COLOR_SAME_AS_FG;
    h->default_fg = dfg; h->default_bg = dbg;
    h->history_rows = (int32_t)s->historybuf->count;
    h->view_offset = (int32_t)s->scrolled_by;
    uint32_t modes = 0;
    if (s->modes.mBRACKETED_PASTE) modes |= KEPM_MODE_BRACKETED_PASTE;
    if (s->modes.mFOCUS_TRACKING) modes |= KEPM_MODE_FOCUS_TRACKING;
    if (s->linebuf == s->alt_linebuf) modes |= KEPM_MODE_ALTERNATE_SCREEN;
    if (s->modes.mDECCKM) modes |= KEPM_MODE_APPLICATION_CURSOR;
    if (reverse_video) modes |= KEPM_MODE_REVERSE_VIDEO;
    h->modes = modes;
    h->mouse_tracking_mode = (uint8_t)s->modes.mouse_tracking_mode;
    h->mouse_tracking_protocol = (uint8_t)s->modes.mouse_tracking_protocol;
    h->key_encoding_flags = s->key_encoding_flags[7] & 0x7f;
    Py_ssize_t tlen = 0;
    const char *title = w->title ? PyUnicode_AsUTF8AndSize(w->title, &tlen) : NULL;
    if (!title) { PyErr_Clear(); tlen = 0; }
    size_t tn = (size_t)tlen < sizeof h->title ? (size_t)tlen : sizeof h->title;
    while (tn && tn < (size_t)tlen && (title[tn] & 0xc0) == 0x80) tn--;  /* do not split a character */
    if (tn) memcpy(h->title, title, tn);
    h->title_len = (uint32_t)tn;
    w->embed.last_title = (uintptr_t)w->title;
    h->frames++;
    atomic_fetch_add_explicit(&h->seq, 1, memory_order_release);   /* even: complete */
    free(row_buf); free(sel);
    w->embed.force = false;
    w->embed.last_cursor_x = cur->x; w->embed.last_cursor_y = cur->y;
    w->embed.last_cursor_visible = screen_is_cursor_visible(s);
    w->embed.last_scrolled_by = s->scrolled_by;
    w->embed.last_focus = s->has_focus;
    return true;
}

static bool
needs_frame(const Window *w, Screen *s) {
    return w->embed.force || s->is_dirty || s->scroll_changed || screen_is_selection_dirty(s)
        || w->embed.last_cursor_x != s->cursor->x || w->embed.last_cursor_y != s->cursor->y
        || w->embed.last_cursor_visible != screen_is_cursor_visible(s)
        || w->embed.last_scrolled_by != s->scrolled_by || w->embed.last_focus != s->has_focus
        || w->embed.last_title != (uintptr_t)w->title;
}

void
export_embedded_windows(void) {
    for (size_t o = 0; o < global_state.num_os_windows; o++) {
        OSWindow *osw = global_state.os_windows + o;
        for (size_t t = 0; t < osw->num_tabs; t++) {
            Tab *tab = osw->tabs + t;
            for (size_t i = 0; i < tab->num_windows; i++) {
                Window *w = tab->windows + i;
                Screen *s = w->render_data.screen;
                if (!w->embed.map || !s) continue;
                if (!needs_frame(w, s)) continue;
                if (write_frame(w, s)) { s->is_dirty = false; s->scroll_changed = false; }
            }
        }
    }
}

void
embed_release(Window *w) {
    if (w->embed.map) munmap(w->embed.map, w->embed.map_size);
    zero_at_ptr(&w->embed);
}

static Window*
window_for_id(id_type id) {
    for (size_t o = 0; o < global_state.num_os_windows; o++) {
        OSWindow *osw = global_state.os_windows + o;
        for (size_t t = 0; t < osw->num_tabs; t++) {
            Tab *tab = osw->tabs + t;
            for (size_t i = 0; i < tab->num_windows; i++) if (tab->windows[i].id == id) return tab->windows + i;
        }
    }
    return NULL;
}

/* embed_attach_map(window_id, fd) -> map size in bytes.
 * The fd is a file the caller created (0600) for this window; it is sized and
 * mapped here, then the caller may close it. */
static PyObject*
embed_attach_map(PyObject *self UNUSED, PyObject *args) {
    unsigned long long id; int fd;
    if (!PyArg_ParseTuple(args, "Ki", &id, &fd)) return NULL;
    Window *w = window_for_id(id);
    if (!w) { PyErr_SetString(PyExc_KeyError, "no such window"); return NULL; }
    const size_t sz = kepm_file_size();
    if (ftruncate(fd, (off_t)sz) != 0) return PyErr_SetFromErrno(PyExc_OSError);
    uint8_t *map = mmap(NULL, sz, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (map == MAP_FAILED) return PyErr_SetFromErrno(PyExc_OSError);
    embed_release(w);
    w->embed.map = map; w->embed.map_size = sz; w->embed.force = true;
    kepm_header *h = (kepm_header*)map;
    memset(h, 0, sizeof *h);
    memcpy(h->magic, KEPM_MAGIC, 4);
    h->version = KEPM_VERSION; h->header_size = KEPM_HEADER_SIZE; h->cell_size = sizeof(kep_cell);
    h->max_cols = KEPM_MAX_COLS; h->max_rows = KEPM_MAX_ROWS; h->combining_capacity = KEPM_COMBINING_CAPACITY;
    h->cells_offset = KEPM_HEADER_SIZE;
    h->dirty_offset = h->cells_offset + KEPM_MAX_COLS * KEPM_MAX_ROWS * (uint32_t)sizeof(kep_cell);
    h->combining_offset = h->dirty_offset + KEPM_MAX_ROWS;
    wakeup_main_loop();
    return PyLong_FromSize_t(sz);
}

static PyObject*
embed_detach_map(PyObject *self UNUSED, PyObject *args) {
    unsigned long long id;
    if (!PyArg_ParseTuple(args, "K", &id)) return NULL;
    Window *w = window_for_id(id);
    if (w) embed_release(w);
    Py_RETURN_NONE;
}

/* embed_set_child_pid(window_id, pid): recorded in the header for the app. */
static PyObject*
embed_set_child_pid(PyObject *self UNUSED, PyObject *args) {
    unsigned long long id; unsigned int pid;
    if (!PyArg_ParseTuple(args, "KI", &id, &pid)) return NULL;
    Window *w = window_for_id(id);
    if (w && w->embed.map) ((kepm_header*)w->embed.map)->child_pid = pid;
    Py_RETURN_NONE;
}

static PyObject*
embed_force_frame(PyObject *self UNUSED, PyObject *args) {
    unsigned long long id;
    if (!PyArg_ParseTuple(args, "K", &id)) return NULL;
    Window *w = window_for_id(id);
    if (w && w->embed.map) { w->embed.force = true; wakeup_main_loop(); }
    Py_RETURN_NONE;
}

static PyMethodDef module_methods[] = {
    METHODB(embed_attach_map, METH_VARARGS),
    METHODB(embed_detach_map, METH_VARARGS),
    METHODB(embed_set_child_pid, METH_VARARGS),
    METHODB(embed_force_frame, METH_VARARGS),
    {NULL, NULL, 0, NULL}
};

bool
init_embed_export(PyObject *module) {
    if (PyModule_AddFunctions(module, module_methods) != 0) return false;
    PyModule_AddIntConstant(module, "EMBED_MAX_COLS", KEPM_MAX_COLS);
    PyModule_AddIntConstant(module, "EMBED_MAX_ROWS", KEPM_MAX_ROWS);
    return true;
}
