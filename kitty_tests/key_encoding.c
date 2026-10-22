// Standalone native regression for the fixed-size key encoding output span.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <string.h>

#include "../kitty/charsets.c"
#include "../kitty/key_encoding.c"

// This fixture exercises ordinary keys only, so no key is a modifier.
bool
is_modifier_key(const uint32_t key UNUSED) {
    return false;
}

typedef struct {
    unsigned char before;
    char output[KEY_BUFFER_SIZE];
    unsigned char after;
} GuardedOutput;

static int
encode_embedded_text(const char *text, GuardedOutput *guard) {
    memset(guard, 0xa5, sizeof(*guard));
    GLFWkeyevent ev = {.key = 'a', .action = GLFW_PRESS, .text = text};
    return encode_glfw_key_event(&ev, false, 0b11000, guard->output);
}

int
main(void) {
    GuardedOutput guard;
    memset(&guard, 0xa5, sizeof(guard));
    GLFWkeyevent ordinary = {.key = 'a', .action = GLFW_PRESS};
    int size = encode_glfw_key_event(&ordinary, false, 0, guard.output);
    assert(size == 1 && guard.output[0] == 'a');
    assert(guard.before == 0xa5 && guard.after == 0xa5);

    size = encode_embedded_text("a", &guard);
    static const char short_expected[] = "\033[97;;97u";
    assert(size == (int)sizeof(short_expected) - 1);
    assert(memcmp(guard.output, short_expected, sizeof(short_expected)) == 0);
    assert(guard.before == 0xa5 && guard.after == 0xa5);

    char fits[41];
    memset(fits, 'a', 40);
    fits[40] = '\0';
    size = encode_embedded_text(fits, &guard);
    assert(size == 126);
    assert(guard.output[size - 1] == 'u' && guard.output[size] == '\0');
    assert(guard.before == 0xa5 && guard.after == 0xa5);

    char too_long[42];
    memset(too_long, 'a', 41);
    too_long[41] = '\0';
    size = encode_embedded_text(too_long, &guard);
    assert(size == KEY_ENCODING_ERROR);
    assert(guard.before == 0xa5 && guard.after == 0xa5);
    return 0;
}
