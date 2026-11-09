# kitty: the Kilix engine fork

This repository is the terminal engine of [Kilix](https://github.com/itsmygithubacct/kilix#readme):
a fork of [kitty](https://github.com/kovidgoyal/kitty) by Kovid Goyal, based on kitty
0.48.2. Branch `main` carries the Kilix changes and is what Kilix pins as its `src`
submodule (Kilix 0.2.3 pins `04c3528c`). Branch `master` follows upstream kitty unchanged.

The fork is built and run only through Kilix, under its own window class, config
directory and build directory, so an installed kitty is left alone. For kitty itself,
read the upstream [README.asciidoc](README.asciidoc) and the
[kitty documentation](https://sw.kovidgoyal.net/kitty/).

## What the fork adds

Most of the change is Python in `kitty/` plus a few kittens; the C core and the
rendering pipeline are upstream kitty.

| Area | Where | What it does |
|---|---|---|
| Pane buttons | `kitty/window_title_bar.py` | maps title-bar cells to actions: the CPU/RAM chip, synchronized input, font size, four split directions, maximize and close; a click on the title opens the pane menu ([pane chrome](https://github.com/itsmygithubacct/kilix/blob/main/docs/pane-chrome.md)) |
| Page strip and status bar | `kitty/tab_bar.py`, `kitty/kilix_chrome/` | pages as tabs with a `+` button, the start key, popups, and status items from registered providers (clock, weather, load, memory) |
| Status samplers | `kitty/kilix_telemetry.py`, `kitty/kilix_system_load.py`, `kitty/kilix_memory.py` | a non-blocking bridge from the chrome to the shared [kilix-telemetry](https://github.com/itsmygithubacct/kilix-telemetry#readme) ring, plus load and memory readers |
| Taskbar | `kitty/kilix_windows.py` | native X11 windows shown as tab-bar entries, so the tab bar is also the taskbar |
| Persistent panes | `kitty/pty_broker.py` | runs each normal pane under a [kitty-pty-broker](https://github.com/itsmygithubacct/kitty-pty-broker#readme) process (overlays stay ordinary children; Kilix's `KILIX_PTY_BROKER=0` turns it off), so a frontend crash detaches panes instead of killing them ([persistent panes](https://github.com/itsmygithubacct/kilix/blob/main/docs/persistent-panes.md)) |
| Voice buttons | `kitty/kilix_voice.py` | read-aloud and dictation as clickable chrome, backed by [kilix-voice](https://github.com/itsmygithubacct/kilix-voice#readme) |
| Virtual machines | `kitty/qemu_shutdown.py` | closing a pane that owns a QEMU monitor offers a guest shutdown, sent as an ACPI power-button request |
| Kittens | `kittens/browse/`, `kittens/kilix_clock/`, `kittens/kilix_chrome_widget/` | `kilix browse` (a browser in a pane), the clock's calendar cards, and the status cards opened by clicking a widget |
| Windows | `kitty/wincompat/`, `glfw/win32_*`, `packaging/windows/`, `kitty/windows_pty_*.py` | the native Windows port used by Kilix for Windows, adapted from [ecstra/win-kitty](https://github.com/ecstra/win-kitty); see [WINDOWS-PORT-NOTICE](WINDOWS-PORT-NOTICE) and [WINDOWS-PTY-BROKER-NOTICE](WINDOWS-PTY-BROKER-NOTICE) |

Tests for these live beside upstream's in `kitty_tests/` (`kilix_*.py`, `pty_broker.py`,
`qemu_shutdown.py`, `windows_*.py`).

## How it is built

Kilix builds the fork; there is no separate install.

1. `kilix --build`, or Kilix's first run, calls Kilix's `build.sh`. It takes a
   `git archive` snapshot of `src/` at the pinned commit, outside the source tree, so
   build products never land beside the sources.
2. The snapshot is compiled with `python3 setup.py build` and the Go toolchain named in
   [`go.mod`](go.mod) (`toolchain go1.27.0`). It needs Python 3.12 or newer with
   headers, a C compiler and kitty's X11 and Wayland development packages.
3. The result goes to `~/.local/gpu_terminal/kilix/build`. If the build fails, Kilix
   falls back to a checksummed prebuilt kitty 0.48.2, which works but has no pane
   buttons.

Plebian-OS builds the fork at first boot (`PLEBIAN_OS_BUILD_KILIX_FORK=1`, with the Go
release and checksum named in its
[release file](https://github.com/itsmygithubacct/plebian-os/blob/main/releases/0.2.3.env)).
Requirements and engine choices are in Kilix's
[install guide](https://github.com/itsmygithubacct/kilix/blob/main/docs/install.md).

To work on the fork directly, build it as upstream kitty is built
([build docs](https://sw.kovidgoyal.net/kitty/build/)) and run it through Kilix with
`KILIX_HOME` pointing at a Kilix checkout whose `src` is this tree.

## Branches

| Branch | Contents |
|---|---|
| `main` | the Kilix engine; every Kilix release pins a commit from it |
| `master` | upstream kitty, not modified here |
| `release/*`, `work/*`, `integrate/*`, `archive/*` | earlier integration and release lines, kept for history |

## Licence

GPL-3.0-only, as kitty; see [LICENSE](LICENSE). kitty is copyright Kovid Goyal and its
contributors. The Windows port keeps the notices in its source files and in
[WINDOWS-PORT-NOTICE](WINDOWS-PORT-NOTICE). Third-party components keep their own
licences.
