"""AI-assisted mapping provider interface.

The AI layer is strictly optional and only used for *ambiguous column mapping*.
It never receives the spreadsheet - only column names, a handful of masked sample
values, and the target field descriptions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.services.mapping_engine import SourceColumn, TargetField

MAX_SAMPLES = 6


@dataclass
class AISuggestion:
    target_field: str | None
    confidence: str = "low"  # "high" | "low"
    reason: str = ""


class AIMappingProvider(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    def suggest(
        self, sources: list[SourceColumn], targets: list[TargetField]
    ) -> dict[str, AISuggestion]: ...


_EMAIL_RE = re.compile(r"^([^@\s])[^@\s]*@([^@\s.]+)(\.[^@\s]+)$")
_DIGITS_RE = re.compile(r"\d")


def mask_value(value: str) -> str:
    """Replace personal data with a shape-preserving mask.

    * emails      -> j***@d***.com
    * digit runs  -> same length digits replaced by 9 (keeps +, spaces, dashes)
    * long text   -> first 2 chars + ***
    """
    v = value.strip()
    m = _EMAIL_RE.match(v)
    if m:
        return f"{m.group(1)}***@{m.group(2)[:1]}***{m.group(3)}"
    if _DIGITS_RE.search(v):
        return _DIGITS_RE.sub("9", v)
    if len(v) > 12:
        return v[:2] + "***"
    return v


def masked_samples(values: list[str], limit: int = MAX_SAMPLES) -> list[str]:
    out: list[str] = []
    for v in values:
        if v is None:
            continue
        masked = mask_value(str(v))
        if masked not in out:
            out.append(masked)
        if len(out) >= limit:
            break
    return out


class NullProvider:
    """Provider used when no API key is configured: always unavailable."""

    name = "none"

    @property
    def available(self) -> bool:
        return False

    def suggest(self, sources, targets):
        return {}
