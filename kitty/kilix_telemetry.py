#!/usr/bin/env python
# License: GPL v3
"""Non-blocking bridge from Kitty chrome to the shared Kilix telemetry ring."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_PACKAGE: ModuleType | bool | None = None
_CLIENT: Any | None = None
_PANE_METRICS: dict[int, Any] = {}
_SNAPSHOT: Any | None = None


def _source_directory(value: str | os.PathLike[str]) -> Path | None:
    candidate = Path(value).expanduser().resolve()
    for source in (candidate, candidate / 'src'):
        if (source / 'kilix_telemetry' / '__init__.py').is_file():
            return source
    return None


def _candidate_sources() -> tuple[Path, ...]:
    values: list[str | os.PathLike[str]] = []
    if explicit := os.environ.get('KILIX_TELEMETRY_SOURCE'):
        values.append(explicit)
    if kilix_home := os.environ.get('KILIX_HOME'):
        values.append(Path(kilix_home) / 'third_party' / 'kilix-telemetry')
    source_home = os.environ.get('GPU_TERMINAL_SOURCE_HOME')
    if source_home:
        values.append(Path(source_home) / 'kilix-modules' / 'kilix-telemetry')
    found: list[Path] = []
    for value in values:
        try:
            source = _source_directory(value)
        except OSError:
            source = None
        if source is not None and source not in found:
            found.append(source)
    return tuple(found)


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _load_from(source: Path) -> ModuleType | None:
    name = 'kilix_telemetry'
    existing = sys.modules.get(name)
    if isinstance(existing, ModuleType):
        location = getattr(existing, '__file__', '')
        if location and _is_below(Path(location), source):
            return existing
        return None
    package = source / name
    spec = importlib.util.spec_from_file_location(
        name,
        package / '__init__.py',
        submodule_search_locations=[str(package)],
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        return None
    return module


def _package() -> ModuleType | None:
    global _PACKAGE
    if isinstance(_PACKAGE, ModuleType):
        return _PACKAGE
    if _PACKAGE is False:
        return None
    sources = _candidate_sources()
    for source in sources:
        if module := _load_from(source):
            _PACKAGE = module
            return module
    # A standalone Kitty installation may use an independently installed
    # package. A Kilix launch always supplies its pinned source above.
    if not sources:
        try:
            module = importlib.import_module('kilix_telemetry')
        except Exception:
            module = None
        if isinstance(module, ModuleType):
            _PACKAGE = module
            return module
    _PACKAGE = False
    return None


def _client() -> Any | None:
    global _CLIENT
    if _CLIENT is None and (package := _package()) is not None:
        try:
            _CLIENT = package.TelemetryClient(cache_seconds=0.0)
        except Exception:
            return None
    return _CLIENT


def refresh_panes(
    root_pids: tuple[int, ...] | list[int], *, register: bool = True
) -> bool:
    """Read one current ring record and cache each requested pane aggregate.

    This never starts a daemon and never invokes the direct collector: Kitty's
    UI thread either gets the already-running shared source or falls back to its
    small local process sampler.
    """
    global _PANE_METRICS, _SNAPSHOT
    roots_set: set[int] = set()
    for value in root_pids:
        try:
            pid = int(value)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            roots_set.add(pid)
    roots = tuple(sorted(roots_set))
    client = _client()
    if client is None:
        _PANE_METRICS = {}
        return False
    try:
        client.register_panes(roots if register else ())
        snapshot = client.snapshot(start=False, fallback=False, force=True)
    except Exception:
        snapshot = None
    if snapshot is None:
        _PANE_METRICS = {}
        return False
    _SNAPSHOT = snapshot
    _PANE_METRICS = {pid: snapshot.pane(pid) for pid in roots}
    return True


def pane_metrics(root_pid: int) -> Any | None:
    try:
        root_pid = int(root_pid)
    except (TypeError, ValueError):
        return None
    return _PANE_METRICS.get(root_pid)


def refresh_global() -> bool:
    global _SNAPSHOT
    client = _client()
    if client is None:
        return False
    try:
        snapshot = client.snapshot(start=False, fallback=False, force=True)
    except Exception:
        snapshot = None
    if snapshot is None:
        return False
    _SNAPSHOT = snapshot
    return True


def hottest_celsius(*, refresh: bool = False) -> float | None:
    if refresh:
        refresh_global()
    if _SNAPSHOT is None:
        return None
    try:
        value = _SNAPSHOT.hottest_celsius
        return None if value is None else float(value)
    except (AttributeError, TypeError, ValueError):
        return None
