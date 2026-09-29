"""Allowlisted campus data tools exposed through one stable factory contract."""

from __future__ import annotations

from functools import partial
from typing import Any

from backend.app.config import Settings
from backend.tools.directory import dept_lookup, find_campus_location
from backend.tools.knowledge import search_academic_knowledge
from backend.tools.public_sources import get_academic_calendar, get_menu, get_notices


def build_tools(settings: Settings) -> dict[str, Any]:
    """Bind runtime settings while keeping the agent-facing arguments URL-free."""

    return {
        "search_academic_knowledge": partial(search_academic_knowledge, settings=settings),
        "get_notices": partial(get_notices, settings=settings),
        "get_academic_calendar": partial(get_academic_calendar, settings=settings),
        "get_menu": partial(get_menu, settings=settings),
        "find_campus_location": partial(find_campus_location, settings=settings),
    }


__all__ = [
    "build_tools",
    "dept_lookup",
    "find_campus_location",
    "get_academic_calendar",
    "get_menu",
    "get_notices",
    "search_academic_knowledge",
]

