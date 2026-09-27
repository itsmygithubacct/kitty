"""Per-tab layout selection and availability from the clickable chrome."""

from typing import TYPE_CHECKING

from .popup import ChromePopup

if TYPE_CHECKING:
    from ..boss import Boss
    from ..window import Window

LAYOUT_ACTION = 'kilix_show_layout_menu'
LAYOUT_LABELS = {
    'splits': 'Splits — drag in all four directions',
    'grid': 'Grid',
    'tall': 'Tall',
    'fat': 'Fat',
    'vertical': 'Vertical',
    'horizontal': 'Horizontal',
    'stack': 'Stack',
}


def show_layout_menu(boss: 'Boss', window: 'Window', manage: bool = False) -> None:
    tab = window.tabref()
    if tab is None:
        return
    owner_id, tab_id = window.id, tab.id
    choices = []
    actions = {}
    for index, (name, label) in enumerate(LAYOUT_LABELS.items(), 1):
        enabled = [item for item in tab.enabled_layouts if item.partition(':')[0] == name]
        if not manage and not enabled:
            continue
        key = str(index)
        if manage:
            mark = '[x]' if enabled else '[ ]'
        else:
            mark = '●' if tab.current_layout.name == name else ' '
        choices.append(f'{key}:{key}. {mark} {label}')
        actions[key] = name
    choices.append('m:Main menu' if manage else 'm:Manage enabled layouts…')

    def selected(key: str) -> None:
        owner = boss.window_id_map.get(owner_id)
        if owner is None or (current := owner.tabref()) is None or current.id != tab_id:
            return
        if key == 'm':
            show_layout_menu(boss, owner, not manage)
            return
        name = actions.get(key)
        if name is None:
            return
        enabled = list(current.enabled_layouts)
        matches = [item for item in enabled if item.partition(':')[0] == name]
        if manage:
            if matches:
                remaining = [item for item in enabled if item not in matches]
                if remaining:
                    # Keep the active variant when disabling a different layout.
                    active = current.current_layout.full_name
                    current.set_enabled_layouts(remaining)
                    if active in remaining:
                        current.goto_layout(active)
            else:
                active = current.current_layout.full_name
                current.set_enabled_layouts([*enabled, name])
                current.goto_layout(active)
            current.mark_tab_bar_dirty()
            show_layout_menu(boss, owner, True)
        elif matches:
            current.goto_layout(matches[0])
            current.mark_tab_bar_dirty()

    from ..fast_data_types import TOP_EDGE, get_options
    edge = 'down' if get_options().tab_bar_edge == TOP_EDGE else 'up'
    title = 'Enabled layouts — this tab (keep at least one)' if manage else 'Layout — this tab'
    boss.choose(
        title, selected, *choices, window=window, title=title,
        name=f'kilix-start-{edge}',
        kilix_popup=ChromePopup(LAYOUT_ACTION, 54, len(choices) + 3),
    )
