/*
 * neuronte_source.c
 * Neuronte as the terminal's only glyph source. See neuronte_source.h.
 *
 * The library is loaded with dlopen(), so a build needs nothing from
 * Neuronte and a run without the switch never looks for it.
 *
 *   KILIX_NEURONTE_ONLY=1      the switch
 *   KILIX_NEURONTE_HOME        a Neuronte tree (build/libneuronte.so, weights/);
 *                              else NEURONTE_HOME; else the installed one,
 *                              $GPU_TERMINAL_SOURCE_HOME/neuronte, which is
 *                              ~/.local/gpu_terminal/sources/neuronte by default
 *
 * Bundles: the newest weights/neuronte-split-vN.nrt first, then every
 * weights/neuronte-term-*.nrt in name order. A character goes to the first
 * of them whose neuronte_untrained() is 0; when none was trained on it the
 * first bundle draws its untrained mark.
 *
 * Distributed under terms of the GPL3 license.
 */

#include "neuronte_source.h"
#include <dirent.h>
#include <dlfcn.h>
#include <math.h>

#define MAX_BUNDLES 24
#define NAME_MAX_LEN 256

typedef struct neuronte neuronte;

static struct {
    int state;  // 0 not asked, 1 on, -1 off
    bool loaded;
    void *lib;
    neuronte *bundles[MAX_BUNDLES];
    unsigned count;
    int (*open_file)(const char *, neuronte **);
    int (*untrained)(const neuronte *, uint32_t);
    int (*set_untrained)(neuronte *, int);
    int (*coverage_styled)(const neuronte *, uint32_t, int, int, int, unsigned, float *);
    float *scratch;
    size_t scratch_sz;
} N;

bool
neuronte_only(void) {
    if (!N.state) {
        const char *v = getenv("KILIX_NEURONTE_ONLY");
        N.state = (v && v[0] == '1' && !v[1]) ? 1 : -1;
    }
    return N.state > 0;
}

static bool
home(char *out, size_t sz) {
    const char *h = getenv("KILIX_NEURONTE_HOME");
    if (!h || !h[0]) h = getenv("NEURONTE_HOME");
    if (h && h[0]) return (size_t)snprintf(out, sz, "%s", h) < sz;
    const char *s = getenv("GPU_TERMINAL_SOURCE_HOME");
    if (s && s[0]) return (size_t)snprintf(out, sz, "%s/neuronte", s) < sz;
    const char *u = getenv("HOME");
    if (!u || !u[0]) return false;
    return (size_t)snprintf(out, sz, "%s/.local/gpu_terminal/sources/neuronte", u) < sz;
}

static int
by_name(const void *a, const void *b) { return strcmp((const char*)a, (const char*)b); }

static void
open_bundle(const char *dir, const char *name) {
    char path[4096];
    if (N.count >= MAX_BUNDLES || (size_t)snprintf(path, sizeof path, "%s/%s", dir, name) >= sizeof path) return;
    neuronte *handle = NULL;
    if (N.open_file(path, &handle) != 0) { log_error("Neuronte: cannot open %s", path); return; }
    N.bundles[N.count++] = handle;
}

static void
load(void) {
    if (N.loaded) return;
    N.loaded = true;
    char root[2048], path[4096], weights[2304];
    if (!home(root, sizeof root)) fatal("KILIX_NEURONTE_ONLY=1 but no Neuronte tree can be named: set KILIX_NEURONTE_HOME");
    snprintf(path, sizeof path, "%s/build/libneuronte.so", root);
    N.lib = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!N.lib) fatal("KILIX_NEURONTE_ONLY=1 but the Neuronte library does not load: %s", dlerror());
#define F(field, name) *(void **)(&N.field) = dlsym(N.lib, name); if (!N.field) fatal("KILIX_NEURONTE_ONLY=1 but %s has no %s (Neuronte 2.2 or later is needed)", path, name);
    F(open_file, "neuronte_open_file"); F(untrained, "neuronte_untrained"); F(set_untrained, "neuronte_set_untrained");
    F(coverage_styled, "neuronte_coverage_styled");
#undef F
    snprintf(weights, sizeof weights, "%s/weights", root);
    static char split[NAME_MAX_LEN], terms[MAX_BUNDLES][NAME_MAX_LEN];
    unsigned num_terms = 0; long best = -1;
    DIR *d = opendir(weights);
    if (d) {
        struct dirent *e;
        while ((e = readdir(d)) != NULL) {
            size_t len = strlen(e->d_name);
            if (len < 5 || len >= NAME_MAX_LEN || strcmp(e->d_name + len - 4, ".nrt") != 0) continue;
            if (strncmp(e->d_name, "neuronte-split-v", 16) == 0) {
                char *end; long v = strtol(e->d_name + 16, &end, 10);
                if (end != e->d_name + 16 && strcmp(end, ".nrt") == 0 && v > best) { best = v; memcpy(split, e->d_name, len + 1); }
            } else if (strncmp(e->d_name, "neuronte-term-", 14) == 0 && num_terms < MAX_BUNDLES - 1) memcpy(terms[num_terms++], e->d_name, len + 1);
        }
        closedir(d);
    }
    if (best < 0) fatal("KILIX_NEURONTE_ONLY=1 but %s has no neuronte-split-vN.nrt", weights);
    open_bundle(weights, split);
    if (!N.count) fatal("KILIX_NEURONTE_ONLY=1 but %s/%s does not open", weights, split);
    // The first bundle draws the mark for what no bundle was trained on.
    if (N.set_untrained(N.bundles[0], 1) != 0) fatal("Neuronte: the untrained mark cannot be selected");
    qsort(terms, num_terms, sizeof terms[0], by_name);
    for (unsigned i = 0; i < num_terms; i++) open_bundle(weights, terms[i]);
}

FontCellMetrics
neuronte_cell_metrics(double font_sz_in_pts, double dpi_x, double dpi_y) {
    (void)dpi_x;
    // The cell of a monospace face is about 0.6 em wide; Neuronte's is twice
    // as high as it is wide. Capitals stand on the row three quarters down.
    double em = font_sz_in_pts * dpi_y / 72.0;
    unsigned w = (unsigned)MAX(2.0, floor(0.6 * em + 0.5)), h = 2 * w;
    unsigned line = (unsigned)MAX(1.0, floor(em / 14.0 + 0.5));
    FontCellMetrics ans = {.cell_width = w, .cell_height = h};
    ans.baseline = (3 * h) / 4;
    ans.underline_thickness = line; ans.strikethrough_thickness = line;
    ans.underline_position = MIN(h - line, ans.baseline + MAX(1u, line));
    ans.strikethrough_position = (11 * h) / 20;
    return ans;
}

// Sprite ids. A character and style gets the next free id the first time
// it is seen; the table holds 65,534 of them, far more than a session
// shows. Past that the last id is shared.
#define SLOTS (1u << 17)
static uint32_t *keys;      // key + 1, 0 is empty
static uint16_t *ids;
static unsigned next_id;

glyph_index
neuronte_glyph_id(char_type ch, bool bold, bool italic) {
    if (!keys) {
        keys = calloc(SLOTS, sizeof keys[0]); ids = calloc(SLOTS, sizeof ids[0]);
        if (!keys || !ids) fatal("Out of memory");
    }
    uint32_t key = ((ch & 0x1fffff) | (bold ? 1u << 21 : 0) | (italic ? 1u << 22 : 0)) + 1;
    unsigned slot = (key * 2654435761u) >> 15;
    while (keys[slot] && keys[slot] != key) slot = (slot + 1) & (SLOTS - 1);
    if (keys[slot]) return ids[slot];
    if (next_id >= 0xfffe) return 0xfffe;
    keys[slot] = key; ids[slot] = (uint16_t)next_id++;
    return ids[slot];
}

static const neuronte*
source(char_type ch) {
    for (unsigned i = 0; i < N.count; i++) if (N.untrained(N.bundles[i], ch) == 0) return N.bundles[i];
    return N.bundles[0];
}

void
neuronte_render_cell(char_type ch, bool bold, bool italic, uint8_t *buf, unsigned width, unsigned height) {
    load();
    size_t area = (size_t)width * height;
    memset(buf, 0, area);
    if (!width || !height || width > 4096 || height > 4096) return;
    if (area > N.scratch_sz) {
        free(N.scratch); N.scratch = malloc(area * sizeof N.scratch[0]);
        if (!N.scratch) fatal("Out of memory");
        N.scratch_sz = area;
    }
    unsigned style = (bold ? 1u : 0u) | (italic ? 2u : 0u);
    if (N.coverage_styled(source(ch), ch, (int)width, (int)height, 0, style, N.scratch) != 0) return;
    for (size_t i = 0; i < area; i++) {
        float v = N.scratch[i];
        buf[i] = v <= 0.f ? 0 : v >= 1.f ? 255 : (uint8_t)(v * 255.f + 0.5f);
    }
}

uint8_t*
neuronte_render_line(const char *text, unsigned cell_width, unsigned cell_height, size_t *width, size_t *height) {
    size_t n = strlen(text);
    if (!n || !cell_width || !cell_height) return NULL;
    uint8_t *ans = calloc(n * cell_width, cell_height), *cell = malloc((size_t)cell_width * cell_height);
    if (!ans || !cell) { free(ans); free(cell); return NULL; }
    for (size_t i = 0; i < n; i++) {
        // Callers pass ASCII; any other byte is shown as the replacement character.
        char_type ch = (unsigned char)text[i] < 0x80 ? (unsigned char)text[i] : 0xfffd;
        neuronte_render_cell(ch, false, false, cell, cell_width, cell_height);
        for (unsigned y = 0; y < cell_height; y++) memcpy(ans + (n * cell_width) * y + i * cell_width, cell + (size_t)cell_width * y, cell_width);
    }
    free(cell);
    *width = n * cell_width; *height = cell_height;
    return ans;
}
