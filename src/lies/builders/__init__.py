"""Source-format builders (PDF, Sphinx, HTML, bespoke).

Builders convert fetched bytes into markdown. They never call an
LLM, never write outside their workspace, never talk to qmd.
"""
from __future__ import annotations
