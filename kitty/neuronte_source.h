/*
 * Neuronte as the terminal's only glyph source.
 *
 * With KILIX_NEURONTE_ONLY=1 in the environment no font file is opened:
 * every cell that holds a character is one Neuronte cell, drawn by
 * neuronte_coverage() (the plain mask, not the draw calls' pixels) from the
 * first bundle trained on that character. A character no bundle was
 * trained on shows the library's own untrained mark.
 *
 * Distributed under terms of the GPL3 license.
 */
#pragma once

#include "data-types.h"

// True when KILIX_NEURONTE_ONLY=1. The library and the bundles are opened
// at the first call that needs them; a failure there is fatal, because no
// other glyph source may take over.
bool neuronte_only(void);

// The cell for this font size: the design cell is 1 wide and 2 high.
FontCellMetrics neuronte_cell_metrics(double font_sz_in_pts, double dpi_x, double dpi_y);

// A sprite-cache id for a character and style. Never 0xffff.
glyph_index neuronte_glyph_id(char_type ch, bool bold, bool italic);

// Coverage of one cell as width * height bytes, 0..255, row-major.
void neuronte_render_cell(char_type ch, bool bold, bool italic, uint8_t *buf, unsigned width, unsigned height);

// One line of text, a cell per character: width * height bytes that the
// caller frees, or NULL.
uint8_t* neuronte_render_line(const char *text, unsigned cell_width, unsigned cell_height, size_t *width, size_t *height);
