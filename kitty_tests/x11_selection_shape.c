// License: GPL v3
// Exercise the MULTIPLE selection property validation without an X server.
#include <assert.h>
#include "../glfw/x11_selection_shape.h"

int main(void) {
    const Atom pair = 77, other = 78;
    assert(x11_multiple_property_shape_ok(pair, pair, 32, 0, 0));       // empty list of pairs
    assert(x11_multiple_property_shape_ok(pair, pair, 32, 0, 2));       // one pair
    assert(x11_multiple_property_shape_ok(pair, pair, 32, 0, 4096));    // many pairs
    assert(!x11_multiple_property_shape_ok(pair, pair, 32, 0, 1));      // half a pair
    assert(!x11_multiple_property_shape_ok(pair, pair, 32, 0, 4095));   // odd count
    assert(!x11_multiple_property_shape_ok(other, pair, 32, 0, 2));     // wrong type
    assert(!x11_multiple_property_shape_ok(None, pair, 32, 0, 2));      // property missing
    assert(!x11_multiple_property_shape_ok(pair, pair, 8, 0, 2));       // wrong item size
    assert(!x11_multiple_property_shape_ok(pair, pair, 16, 0, 2));
    assert(!x11_multiple_property_shape_ok(pair, pair, 32, 4, 2));      // truncated read, more data left
    return 0;
}
