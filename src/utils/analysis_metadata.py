# -*- coding: utf-8 -*-
"""
Shared metadata constants for analysis requests.
"""

from __future__ import annotations


SELECTION_SOURCES: tuple[str, ...] = ("manual", "autocomplete", "import", "image")
SELECTION_SOURCE_PATTERN = "^(" + "|".join(SELECTION_SOURCES) + ")$"

# Research route identity is deliberately separate from UI/request origin.
RESEARCH_SELECTION_SOURCES: tuple[str, ...] = ("AUTO_SCREEN", "SPECIFIED_CODES")
