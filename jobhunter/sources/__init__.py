"""Source registry."""
from __future__ import annotations

from .ats import (ATS_SOURCES, AshbySource, GreenhouseSource, LeverSource, RecruiteeSource,
                  SmartRecruitersSource, WorkableSource, WorkdaySource)
from .base import Source
from .boards import PUBLIC_SOURCES
from .jobspy_source import JobSpySource
from .ycombinator import YCombinatorSource

ALL_SOURCE_CLASSES: list[type[Source]] = [*PUBLIC_SOURCES, *ATS_SOURCES,
                                          YCombinatorSource, JobSpySource]

REGISTRY: dict[str, type[Source]] = {cls.name: cls for cls in ALL_SOURCE_CLASSES}


def build_sources(only: list[str] | None = None, skip: list[str] | None = None) -> list[Source]:
    names = list(REGISTRY)
    if only:
        names = [n for n in names if n in set(only)]
    if skip:
        names = [n for n in names if n not in set(skip)]
    return [REGISTRY[n]() for n in names]


__all__ = ["Source", "REGISTRY", "build_sources", "ALL_SOURCE_CLASSES",
           "GreenhouseSource", "LeverSource", "AshbySource", "WorkdaySource",
           "SmartRecruitersSource", "WorkableSource", "RecruiteeSource",
           "YCombinatorSource", "JobSpySource"]
