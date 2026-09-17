"""The `PluginContext` core passes with every call, where process-wide code can read it.

Core hands each handler a ``context``. The cookie store is process-wide and
cannot take it as an argument, so the handler binds it here first and the store
reads it back.

Outside the engine nothing is bound — the ``logos-login`` console script, tests —
and the data directory resolves by the rule core itself uses, so a login from the
terminal writes exactly where the running server reads.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from research_engine_sdk import PluginContext

PLUGIN_ID = "logos"

_bound: PluginContext | None = None


def bind_context(context: PluginContext | None) -> None:
    """Remember the context core passed. ``None`` leaves the current binding alone."""
    global _bound
    if context is not None:
        _bound = context


def reset_context() -> None:
    """Forget the bound context. For tests."""
    global _bound
    _bound = None


def current_context() -> PluginContext | None:
    return _bound


def resolve_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Where core puts this plugin's data, worked out without importing core.

    Core's plugin loader passes ``settings.data_dir / "plugin-data" / plugin_id``,
    and ``settings.data_dir`` is ``RE_DATA_DIR`` or ``~/.research-engine``. Read
    at call time, so a test's ``HOME`` or ``RE_DATA_DIR`` takes effect.
    """
    env = os.environ if environ is None else environ
    root = env.get("RE_DATA_DIR")
    base = Path(root).expanduser() if root else Path.home() / ".research-engine"
    return base / "plugin-data" / PLUGIN_ID


def data_dir() -> Path:
    """This plugin's mutable-state directory. Not created here."""
    if _bound is not None:
        return _bound.data_dir
    return resolve_data_dir()


def binds_context(
    handler: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Bind the ``context`` keyword core passes before *handler* runs.

    Sits under ``@tool``. ``functools.wraps`` keeps the handler's signature
    visible to core's introspection.
    """

    @functools.wraps(handler)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        bind_context(kwargs.get("context"))
        return await handler(*args, **kwargs)

    return wrapper
