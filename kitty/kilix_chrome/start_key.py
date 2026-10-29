"""Recognize a lone Super release without consuming any Super combination."""

from ..fast_data_types import (
    GLFW_FKEY_LEFT_SUPER,
    GLFW_FKEY_RIGHT_SUPER,
    GLFW_MOD_ALT,
    GLFW_MOD_CONTROL,
    GLFW_MOD_HYPER,
    GLFW_MOD_META,
    GLFW_MOD_SHIFT,
    GLFW_PRESS,
    GLFW_RELEASE,
    KeyEvent,
)

SUPER_KEYS = frozenset((GLFW_FKEY_LEFT_SUPER, GLFW_FKEY_RIGHT_SUPER))
OTHER_MODIFIERS = GLFW_MOD_ALT | GLFW_MOD_CONTROL | GLFW_MOD_SHIFT | GLFW_MOD_HYPER | GLFW_MOD_META


class LoneSuperKey:
    def __init__(self) -> None:
        self.pressed: set[int] = set()
        self.candidate: tuple[int, int] | None = None

    def cancel(self) -> None:
        self.candidate = None

    def reset(self) -> None:
        self.pressed.clear()
        self.cancel()

    def event(self, event: KeyEvent, owner: tuple[int, int]) -> tuple[bool, bool]:
        """Return (consume raw modifier, open Start); ordinary keys pass through."""
        if event.key not in SUPER_KEYS:
            self.cancel()
            return False, False
        if event.action == GLFW_PRESS and event.key not in self.pressed:
            self.candidate = owner if not self.pressed and not event.mods & OTHER_MODIFIERS else None
            self.pressed.add(event.key)
        elif event.action == GLFW_RELEASE:
            activate = event.key in self.pressed and self.candidate == owner and not event.mods & OTHER_MODIFIERS
            self.pressed.discard(event.key)
            self.cancel()
            return True, activate
        # Repeats neither trigger Start nor arm an orphaned key after focus loss.
        return True, False
