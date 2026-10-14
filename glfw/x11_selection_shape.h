// Validation of the property a MULTIPLE selection request names. Kept free of
// GLFW state so that a test can compile it on its own.
#pragma once
#include <X11/Xlib.h>
#include <stdbool.h>

// A MULTIPLE request's property must be an ATOM_PAIR property of 32-bit items
// ((target, property) pairs), read completely, with an even number of items.
static inline bool
x11_multiple_property_shape_ok(Atom actual_type, Atom atom_pair, int actual_format, unsigned long bytes_after, unsigned long item_count) {
    return actual_type == atom_pair && actual_format == 32 && bytes_after == 0 && (item_count & 1) == 0;
}
