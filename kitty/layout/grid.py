#!/usr/bin/env python
# License: GPLv3 Copyright: 2020, Kovid Goyal <kovid at kovidgoyal.net>

from collections.abc import Callable, Generator, Iterator, Sequence
from functools import lru_cache
from itertools import repeat
from math import ceil, floor
from typing import Any

from kitty.borders import BorderColor
from kitty.types import Edges, NeighborsMap, WindowMapper
from kitty.typing_compat import WindowType
from kitty.window_list import WindowGroup, WindowList

from .base import BorderLine, DragOverlayMode, Layout, LayoutData, LayoutDimension, ListOfWindows, layout_dimension, lgd
from .tall import neighbors_for_tall_window


@lru_cache
def calc_grid_size(n: int) -> tuple[int, int, int, int]:
    if n <= 5:
        ncols = 1 if n == 1 else 2
    else:
        for ncols in range(3, (n // 2) + 1):
            if ncols * ncols >= n:
                break
    nrows = n // ncols
    special_rows = n - (nrows * (ncols - 1))
    special_col = 0 if special_rows < nrows else ncols - 1
    return ncols, nrows, special_rows, special_col


SESSION_COLUMNS = 4


@lru_cache
def sessions_column_heights(n: int) -> tuple[int, ...]:
    # Columns grow to four before a new row. Panes fill left to right, so a
    # short last row is the leftmost columns and adding a pane never moves an
    # earlier one. Twelve panes are four columns of three.
    if n <= 0:
        return ()
    ncols = min(n, SESSION_COLUMNS)
    full, extra = divmod(n, ncols)
    return tuple(full + 1 if col < extra else full for col in range(ncols))


@lru_cache
def calc_sessions_grid_size(n: int) -> tuple[int, int, int, int]:
    # Bounding columns and rows. One special column cannot describe every
    # count (six panes are 2, 2, 1, 1); Sessions places from the heights.
    heights = sessions_column_heights(n) or (1,)
    rows = max(heights)
    return len(heights), rows, rows, 0


class Grid(Layout):

    name: str = 'grid'
    no_minimal_window_borders = True
    drag_overlay_mode = DragOverlayMode.axis_y

    def grid_size(self, n: int) -> tuple[int, int, int, int]:
        return calc_grid_size(n)

    def remove_all_biases(self) -> bool:
        self.biased_rows: dict[int, float] = {}
        self.biased_cols: dict[int, float] = {}
        return True

    def column_layout(
        self,
        num: int,
        bias: Sequence[float] | None = None,
    ) -> LayoutDimension:
        decoration_pairs = tuple(repeat((0, 0), num))
        return layout_dimension(lgd.central.left, lgd.central.width, lgd.cell_width, decoration_pairs, bias=bias, alignment=lgd.alignment_x)

    def row_layout(
        self,
        num: int,
        bias: Sequence[float] | None = None,
    ) -> LayoutDimension:
        decoration_pairs = tuple(repeat((0, 0), num))
        return layout_dimension(lgd.central.top, lgd.central.height, lgd.cell_height, decoration_pairs, bias=bias, alignment=lgd.alignment_y)

    def variable_layout(self, layout_func: Callable[..., LayoutDimension], num_windows: int, biased_map: dict[int, float]) -> LayoutDimension:
        return layout_func(num_windows, bias=biased_map if num_windows > 1 else None)

    def position_for_window_idx(self, idx: int, num_windows: int, ncols:int , nrows: int, special_rows: int, special_col: int) -> tuple[int, int]:
        row_num = col_num = 0

        def on_col_done(col_windows: list[int]) -> None:
            nonlocal col_num, row_num
            row_num = 0
            col_num += 1

        for window_idx, xl, yl in self.layout_windows(
                num_windows, nrows, ncols, special_rows, special_col, on_col_done):
            if idx == window_idx:
                return row_num, col_num
            row_num += 1
        return 0, 0

    def bias_slot(self, all_windows: WindowList, idx: int, fractional_bias: float, cell_increment_bias_h: float, cell_increment_bias_v: float) -> bool:
        num_windows = all_windows.num_groups
        ncols, nrows, special_rows, special_col = self.grid_size(num_windows)
        row_num, col_num = self.position_for_window_idx(idx, num_windows, ncols, nrows, special_rows, special_col)
        if row_num == 0:
            b = self.biased_cols
            layout_func = self.column_layout
            bias_idx = col_num
            increment = cell_increment_bias_h
        else:
            b = self.biased_rows
            layout_func = self.row_layout
            bias_idx = row_num
            increment = cell_increment_bias_v
        before_layout = tuple(self.variable_layout(layout_func, num_windows, b))
        b[bias_idx] = increment
        return tuple(self.variable_layout(layout_func, num_windows, b)) == before_layout

    def apply_bias(self, window_id: int, increment: float, all_windows: WindowList, is_horizontal: bool = True) -> bool:
        num_windows = all_windows.num_groups
        ncols, nrows, special_rows, special_col = self.grid_size(num_windows)
        row_num, col_num = self.position_for_window_idx(window_id, num_windows, ncols, nrows, special_rows, special_col)

        if is_horizontal:
            b = self.biased_cols
            if ncols < 2:
                return False
            bias_idx = col_num
            attr = 'biased_cols'

            def layout_func(windows: ListOfWindows, bias: Sequence[float] | None = None) -> LayoutDimension:
                return self.column_layout(num_windows, bias=bias)

        else:
            b = self.biased_rows
            if max(nrows, special_rows) < 2:
                return False
            bias_idx = row_num
            attr = 'biased_rows'

            def layout_func(windows: ListOfWindows, bias: Sequence[float] | None = None) -> LayoutDimension:
                return self.row_layout(num_windows, bias=bias)

        before_layout = tuple(self.variable_layout(layout_func, num_windows, b))
        candidate = b.copy()
        before = candidate.get(bias_idx, 0)
        candidate[bias_idx] = before + increment
        if before_layout == tuple(self.variable_layout(layout_func, num_windows, candidate)):
            return False
        setattr(self, attr, candidate)
        return True

    def layout_windows(
        self,
        num_windows: int,
        nrows: int, ncols: int,
        special_rows: int, special_col: int,
        on_col_done: Callable[[list[int]], None] = lambda col_windows: None
    ) -> Generator[tuple[int, LayoutData, LayoutData], None, None]:
        # Distribute windows top-to-bottom, left-to-right (i.e. in columns)
        xlayout = self.variable_layout(self.column_layout, ncols, self.biased_cols)
        yvals_normal = tuple(self.variable_layout(self.row_layout, nrows, self.biased_rows))
        yvals_special = yvals_normal if special_rows == nrows else tuple(self.variable_layout(self.row_layout, special_rows, self.biased_rows))
        pos = 0
        for col in range(ncols):
            rows = special_rows if col == special_col else nrows
            yls = yvals_special if col == special_col else yvals_normal
            xl = next(xlayout)
            col_windows = []
            for i, yl in enumerate(yls):
                window_idx = pos + i
                yield window_idx, xl, yl
                col_windows.append(window_idx)
            pos += rows
            on_col_done(col_windows)

    def do_layout(self, windows: WindowList) -> None:
        n = windows.num_groups
        if n == 1:
            self.layout_single_window_group(next(windows.iter_all_layoutable_groups()))
            return
        ncols, nrows, special_rows, special_col = self.grid_size(n)
        groups = tuple(windows.iter_all_layoutable_groups())
        win_col_map: list[list[WindowGroup]] = []

        def on_col_done(col_windows: list[int]) -> None:
            col_windows_w = [groups[i] for i in col_windows]
            win_col_map.append(col_windows_w)

        def extents(ld: LayoutData) -> tuple[int, int]:
            start = ld.content_pos - ld.space_before
            size = ld.space_before + ld.space_after + ld.content_size
            return start, size

        def layout(ld: LayoutData, cell_length: int, before_dec: int, after_dec: int) -> LayoutData:
            start, size = extents(ld)
            space_needed_for_decorations = before_dec + after_dec
            content_size = size - space_needed_for_decorations
            number_of_cells = content_size // cell_length
            cell_area = number_of_cells * cell_length
            extra = content_size - cell_area
            if lgd.alignment_x == 0:  # center
                before_dec += extra // 2
            elif lgd.alignment_x > 0:  # end
                before_dec += extra
            return LayoutData(start + before_dec, number_of_cells, before_dec, size - cell_area - before_dec, cell_area)

        def position_window_in_grid_cell(window_idx: int, xl: LayoutData, yl: LayoutData) -> None:
            wg = groups[window_idx]
            edges = Edges(wg.decoration('left'), wg.decoration('top'), wg.decoration('right'), wg.decoration('bottom'))
            xl = layout(xl, lgd.cell_width, edges.left, edges.right)
            yl = layout(yl, lgd.cell_height, edges.top, edges.bottom)
            self.set_window_group_geometry(wg, xl, yl)

        for window_idx, xl, yl in self.layout_windows(
                n, nrows, ncols, special_rows, special_col, on_col_done):
            position_window_in_grid_cell(window_idx, xl, yl)

    def minimal_borders(self, windows: WindowList) -> Iterator[BorderLine]:
        n = windows.num_groups
        if not lgd.draw_minimal_borders or n < 2:
            return
        needs_borders_map = windows.compute_needs_borders_map(lgd.draw_active_borders)
        ncols, nrows, special_rows, special_col = self.grid_size(n)
        is_first_row: set[int] = set()
        is_last_row: set[int] = set()
        is_first_column: set[int] = set()
        is_last_column: set[int] = set()
        groups = tuple(windows.iter_all_layoutable_groups())
        bw = groups[0].effective_border()
        if not bw:
            return
        xl: LayoutData = LayoutData()
        yl: LayoutData = LayoutData()
        prev_col_windows: list[int] = []
        layout_data_map: dict[int, tuple[LayoutData, LayoutData]] = {}

        def on_col_done(col_windows: list[int]) -> None:
            nonlocal prev_col_windows, is_first_column
            if col_windows:
                is_first_row.add(groups[col_windows[0]].id)
                is_last_row.add(groups[col_windows[-1]].id)
            if not prev_col_windows:
                is_first_column = {groups[x].id for x in col_windows}
            prev_col_windows = col_windows

        all_groups_in_order: list[WindowGroup] = []
        for window_idx, xl, yl in self.layout_windows(n, nrows, ncols, special_rows, special_col, on_col_done):
            wg = groups[window_idx]
            all_groups_in_order.append(wg)
            layout_data_map[wg.id] = xl, yl
        is_last_column = {groups[x].id for x in prev_col_windows}
        active_group = windows.active_group

        def ends(yl: LayoutData) -> tuple[int, int]:
            return yl.content_pos - yl.space_before, yl.content_pos + yl.content_size + yl.space_after

        def borders_for_window(gid: int, color: BorderColor, wid: int) -> Iterator[BorderLine]:
            xl, yl = layout_data_map[gid]
            left, right = ends(xl)
            top, bottom = ends(yl)
            first_row, last_row = gid in is_first_row, gid in is_last_row
            first_column, last_column = gid in is_first_column, gid in is_last_column

            # Horizontal
            if not first_row:
                yield BorderLine(Edges(left, top, right, top + bw), color, -wid, True)
            if not last_row:
                yield BorderLine(Edges(left, bottom - bw, right, bottom), color, wid, True)

            # Vertical
            if not first_column:
                yield BorderLine(Edges(left, top, left + bw, bottom), color, -wid, False)
            if not last_column:
                yield BorderLine(Edges(right - bw, top, right, bottom), color, wid, False)

        for wg in all_groups_in_order:
            color = BorderColor.inactive
            if needs_borders_map.get(wg.id):
                color = BorderColor.active if wg is active_group else BorderColor.bell
            wid = wg.active_window_id
            yield from borders_for_window(wg.id, color, wid)

    def neighbors_for_window(self, window: WindowType, windows: WindowList) -> NeighborsMap:
        n = windows.num_groups
        if n < 4:
            return neighbors_for_tall_window(1, window, windows)
        return self.grid_neighbors(window, windows, n)

    def grid_neighbors(self, window: WindowType, windows: WindowList, n: int) -> NeighborsMap:
        wg = windows.group_for_window(window)
        assert wg is not None
        ncols, nrows, special_rows, special_col = self.grid_size(n)
        blank_row: list[int | None] = [None for i in range(ncols)]
        matrix = tuple(blank_row[:] for j in range(max(nrows, special_rows)))
        wi = windows.iter_all_layoutable_groups()
        pos_map: dict[int, tuple[int, int]] = {}
        col_counts: list[int] = []
        for col in range(ncols):
            rows = special_rows if col == special_col else nrows
            for row in range(rows):
                w = next(wi)
                matrix[row][col] = wid = w.id
                pos_map[wid] = row, col
            col_counts.append(rows)
        row, col = pos_map[wg.id]

        def neighbors(row: int, col: int) -> list[int]:
            try:
                ans = matrix[row][col]
            except IndexError:
                ans = None
            return [] if ans is None else [ans]

        def side(row: int, col: int, delta: int) -> list[int]:
            neighbor_col = col + delta
            neighbor_nrows = col_counts[neighbor_col]
            nrows = col_counts[col]
            if neighbor_nrows == nrows:
                return neighbors(row, neighbor_col)

            start_row = floor(neighbor_nrows * row / nrows)
            end_row = ceil(neighbor_nrows * (row + 1) / nrows)
            xs = []
            for neighbor_row in range(start_row, end_row):
                xs.extend(neighbors(neighbor_row, neighbor_col))
            return xs

        ans: NeighborsMap = {}
        if row:
            ans['top'] = neighbors(row-1, col)
        if bottom := neighbors(row + 1, col):
            ans['bottom'] = bottom
        if col and (left := side(row, col, -1)):
            ans['left'] = left
        if col < ncols - 1:
            ans['right'] = side(row, col, 1)
        return ans

    def layout_state(self) -> dict[str, Any]:
        return {
            'biased_cols': self.biased_cols,
            'biased_rows': self.biased_rows
        }

    def set_layout_state(self, layout_state: dict[str, Any], map_group_id: WindowMapper) -> bool:
        self.biased_rows = {int(k): v for k, v in layout_state['biased_rows'].items()}
        self.biased_cols = {int(k): v for k, v in layout_state['biased_cols'].items()}
        return True


class Sessions(Grid):
    """Coding sessions: up to four columns, then another row."""

    name = 'sessions'
    explicit_only = True

    def grid_size(self, n: int) -> tuple[int, int, int, int]:
        return calc_sessions_grid_size(n)

    def layout_windows(
        self,
        num_windows: int,
        nrows: int, ncols: int,
        special_rows: int, special_col: int,
        on_col_done: Callable[[list[int]], None] = lambda col_windows: None
    ) -> Generator[tuple[int, LayoutData, LayoutData], None, None]:
        # Left to right, then the next row, on one shared set of rows. The
        # special-column arguments cannot describe a partial row (six panes
        # are 2, 2, 1, 1). A short column's last pane spans the rows below it.
        heights = sessions_column_heights(num_windows)
        if not heights:
            return
        nrows = max(heights)
        xlayout = self.variable_layout(self.column_layout, len(heights), self.biased_cols)
        yvals = tuple(self.variable_layout(self.row_layout, nrows, self.biased_rows))
        for col, rows in enumerate(heights):
            xl = next(xlayout)
            col_windows: list[int] = []
            for row in range(rows):
                yl = yvals[row] if row < rows - 1 or row == nrows - 1 else self._span_rows(yvals[row:])
                window_idx = row * len(heights) + col
                yield window_idx, xl, yl
                col_windows.append(window_idx)
            on_col_done(col_windows)

    @staticmethod
    def _span_rows(cells: Sequence[LayoutData]) -> LayoutData:
        first, last = cells[0], cells[-1]
        if len(cells) == 1:
            return first
        end = last.content_pos + last.content_size
        return LayoutData(
            first.content_pos,
            sum(cell.cells_per_window for cell in cells),
            first.space_before,
            last.space_after,
            end - first.content_pos,
        )

    def neighbors_for_window(self, window: WindowType, windows: WindowList) -> NeighborsMap:
        n = windows.num_groups
        # Tall's small-count shortcut is side-by-side only. From two panes on
        # this grid has up to four columns and then another row.
        return self.grid_neighbors(window, windows, n) if n > 1 else {}

    def grid_neighbors(self, window: WindowType, windows: WindowList, n: int) -> NeighborsMap:
        wg = windows.group_for_window(window)
        assert wg is not None
        heights = sessions_column_heights(n)
        if not heights:
            return {}
        ncols = len(heights)
        nrows = max(heights)
        # A short column's last pane covers every grid row from its own row down.
        coverage: list[list[int | None]] = [[None for i in range(ncols)] for j in range(nrows)]
        wi = windows.iter_all_layoutable_groups()
        origin: dict[int, tuple[int, int]] = {}
        for index in range(n):
            w = next(wi)
            row, col = divmod(index, ncols)
            end = nrows - 1 if row == heights[col] - 1 else row
            for grid_row in range(row, end + 1):
                coverage[grid_row][col] = w.id
            origin[w.id] = row, col
        if wg.id not in origin:
            return {}
        row, col = origin[wg.id]
        covered = [grid_row for grid_row in range(nrows) if coverage[grid_row][col] == wg.id]
        top_row, bot_row = covered[0], covered[-1]

        def beside(delta: int) -> list[int]:
            neighbor_col = col + delta
            seen: list[int] = []
            for grid_row in range(top_row, bot_row + 1):
                wid = coverage[grid_row][neighbor_col]
                if wid is not None and wid not in seen:
                    seen.append(wid)
            return seen

        ans: NeighborsMap = {}
        if top_row and (above := coverage[top_row - 1][col]) is not None:
            ans['top'] = [above]
        if bot_row + 1 < nrows and (below := coverage[bot_row + 1][col]) is not None:
            ans['bottom'] = [below]
        if col and (left := beside(-1)):
            ans['left'] = left
        if col < ncols - 1 and (right := beside(1)):
            ans['right'] = right
        return ans

    def apply_bias(self, window_id: int, increment: float, all_windows: WindowList, is_horizontal: bool = True) -> bool:
        # Grid probes a bias against one slot per window. Sessions have at
        # most four columns, and a second row only from the fifth pane on.
        num_windows = all_windows.num_groups
        heights = sessions_column_heights(num_windows)
        ncols = len(heights)
        nrows = max(heights) if heights else 1
        row_num, col_num = self.position_for_window_idx(window_id, num_windows, ncols, nrows, nrows, 0)
        if is_horizontal:
            attr, bias_idx, slots, layout_func = 'biased_cols', col_num, ncols, self.column_layout
        else:
            attr, bias_idx, slots, layout_func = 'biased_rows', row_num, nrows, self.row_layout
        if slots < 2:
            return False
        current: dict[int, float] = getattr(self, attr)
        candidate = current.copy()
        candidate[bias_idx] = candidate.get(bias_idx, 0) + increment
        if tuple(self.variable_layout(layout_func, slots, current)) == tuple(self.variable_layout(layout_func, slots, candidate)):
            return False
        setattr(self, attr, candidate)
        return True
