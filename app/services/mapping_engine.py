"""Auto-mapping engine: source columns -> target schema fields.

Resolution order (first confident hit wins):

1. saved mapping (mapping memory / template)   -> HIGH
2. exact name match                            -> HIGH
3. normalized name match (case/punctuation)    -> HIGH
4. alias match                                 -> HIGH
5. fuzzy match (RapidFuzz) + type hint         -> MEDIUM / LOW
6. AI suggestion (optional, only if 1-5 fail)  -> MEDIUM / LOW

Confidence is categorical on purpose - HIGH / MEDIUM / LOW / UNMAPPED - because a
"93.71 %" would be false precision. LOW suggestions must be confirmed by the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.services.ai.base import AIMappingProvider, AISuggestion

HIGH, MEDIUM, LOW, UNMAPPED = "HIGH", "MEDIUM", "LOW", "UNMAPPED"

FUZZY_MEDIUM = 88
FUZZY_LOW = 72

_NOISE_WORDS = {"the", "of", "a", "an", "no", "num", "number", "nr", "col", "column", "field"}

# detected source type -> compatible target field types
_TYPE_COMPAT = {
    "email": {"email"},
    "phone": {"phone"},
    "date": {"date", "datetime"},
    "boolean": {"boolean"},
    "url": {"url"},
    "integer": {"integer", "decimal", "string"},
    "decimal": {"decimal"},
    "country_code": {"country", "currency", "enum", "string"},
}


@dataclass
class TargetField:
    name: str
    display_name: str = ""
    type: str = "string"
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    allow_multiple_sources: bool = False
    required: bool = False

    @classmethod
    def from_model(cls, f) -> TargetField:
        return cls(
            name=f.name,
            display_name=f.display_name or "",
            type=f.type,
            description=f.description or "",
            aliases=list(f.aliases or []),
            allow_multiple_sources=bool(f.allow_multiple_sources),
            required=bool(f.required),
        )


@dataclass
class SourceColumn:
    name: str
    detected_type: str = "string"
    samples: list[str] = field(default_factory=list)


@dataclass
class MappingSuggestion:
    source_column: str
    target_field: str | None
    confidence: str
    method: str
    score: float = 0.0

    def as_dict(self) -> dict:
        return {
            "source_column": self.source_column,
            "target_field": self.target_field,
            "confidence": self.confidence,
            "method": self.method,
        }


def normalize_header(name: str) -> str:
    """Canonical form of a header: lowercase, alphanumerics only, no noise words."""
    s = name.strip().lower()
    s = re.sub(r"[\s_\-./\\()\[\]:#'\"]+", " ", s)
    s = re.sub(r"[^a-z0-9а-яё֐-׿؀-ۿ ]+", "", s)
    words = [w for w in s.split() if w not in _NOISE_WORDS]
    return "".join(words) if words else re.sub(r"\s+", "", s)


def _field_keys(f: TargetField) -> dict[str, str]:
    """All normalized names of a field -> which kind they are."""
    keys: dict[str, str] = {normalize_header(f.name): "exact"}
    if f.display_name:
        keys.setdefault(normalize_header(f.display_name), "normalized")
    # allow "customer_name" == "customername" == "name of customer"
    keys.setdefault(normalize_header(f.name.replace("_", " ")), "normalized")
    for alias in f.aliases:
        keys.setdefault(normalize_header(alias), "alias")
    return keys


def _fuzzy_score(src: SourceColumn, f: TargetField) -> float:
    candidates = [f.name.replace("_", " "), f.display_name, *f.aliases]
    best = 0.0
    src_text = src.name.replace("_", " ")
    for cand in candidates:
        if not cand:
            continue
        score = max(
            fuzz.token_set_ratio(src_text, cand),
            fuzz.WRatio(src_text, cand),
        )
        best = max(best, score)
    compat = _TYPE_COMPAT.get(src.detected_type)
    if compat is not None:
        if f.type in compat:
            best = min(100.0, best + 6)
        elif f.type not in ("string", "enum"):
            best -= 15
    return best


class MappingEngine:
    def __init__(self, ai_provider: AIMappingProvider | None = None):
        self.ai_provider = ai_provider

    def suggest(
        self,
        sources: list[SourceColumn],
        targets: list[TargetField],
        memory: dict[str, str] | None = None,
        use_ai: bool = False,
    ) -> list[MappingSuggestion]:
        memory = memory or {}
        target_by_name = {t.name: t for t in targets}
        keys_by_target = {t.name: _field_keys(t) for t in targets}

        proposals: list[MappingSuggestion] = []
        unresolved: list[SourceColumn] = []

        # deterministic levels ------------------------------------------------
        for src in sources:
            key = normalize_header(src.name)
            saved = memory.get(key) or memory.get(src.name)
            if saved and saved in target_by_name:
                proposals.append(MappingSuggestion(src.name, saved, HIGH, "saved", 100))
                continue
            found = None
            for tname, keys in keys_by_target.items():
                if key in keys:
                    kind = keys[key]
                    found = MappingSuggestion(src.name, tname, HIGH, kind, 100)
                    if kind == "exact":
                        break
                    # prefer exact when several fields collide on the same key
            if found:
                proposals.append(found)
            else:
                unresolved.append(src)

        # fuzzy level -----------------------------------------------------------
        fuzzy_candidates: list[tuple[float, SourceColumn, TargetField]] = []
        for src in unresolved:
            for t in targets:
                score = _fuzzy_score(src, t)
                if score >= FUZZY_LOW:
                    fuzzy_candidates.append((score, src, t))
        fuzzy_candidates.sort(key=lambda x: -x[0])

        taken_targets = {p.target_field for p in proposals if p.target_field}
        taken_targets = {t for t in taken_targets if not target_by_name[t].allow_multiple_sources}
        fuzzy_done: set[str] = set()
        for score, src, t in fuzzy_candidates:
            if src.name in fuzzy_done:
                continue
            if t.name in taken_targets and not t.allow_multiple_sources:
                continue
            conf = MEDIUM if score >= FUZZY_MEDIUM else LOW
            proposals.append(MappingSuggestion(src.name, t.name, conf, "fuzzy", score))
            fuzzy_done.add(src.name)
            if not t.allow_multiple_sources:
                taken_targets.add(t.name)
        still_unresolved = [s for s in unresolved if s.name not in fuzzy_done]

        # type inference: single free target of a distinctive type ---------------
        for src in list(still_unresolved):
            compat = _TYPE_COMPAT.get(src.detected_type)
            if not compat or src.detected_type in ("integer", "decimal", "country_code"):
                continue
            free = [t for t in targets if t.type in compat and t.name not in taken_targets]
            if len(free) == 1:
                proposals.append(MappingSuggestion(src.name, free[0].name, LOW, "type", 60))
                taken_targets.add(free[0].name)
                still_unresolved.remove(src)

        # AI level ------------------------------------------------------------------
        if still_unresolved and use_ai and self.ai_provider and self.ai_provider.available:
            free_targets = [t for t in targets if t.name not in taken_targets]
            if free_targets:
                try:
                    ai_results = self.ai_provider.suggest(still_unresolved, free_targets)
                except Exception:  # provider failure must never break the flow
                    ai_results = {}
                for src in list(still_unresolved):
                    sug: AISuggestion | None = ai_results.get(src.name)
                    if sug and sug.target_field in target_by_name:
                        if sug.target_field in taken_targets:
                            continue
                        conf = MEDIUM if sug.confidence == "high" else LOW
                        proposals.append(
                            MappingSuggestion(src.name, sug.target_field, conf, "ai", 50)
                        )
                        taken_targets.add(sug.target_field)
                        still_unresolved.remove(src)

        for src in still_unresolved:
            proposals.append(MappingSuggestion(src.name, None, UNMAPPED, "none", 0))

        order = {s.name: i for i, s in enumerate(sources)}
        proposals.sort(key=lambda p: order[p.source_column])
        return proposals


def find_conflicts(mapping: dict[str, str | None], targets: list[TargetField]) -> list[dict]:
    """Targets that are mapped from more than one source without allowing it."""
    allow = {t.name: t.allow_multiple_sources for t in targets}
    by_target: dict[str, list[str]] = {}
    for src, tgt in mapping.items():
        if tgt:
            by_target.setdefault(tgt, []).append(src)
    return [
        {"target_field": tgt, "sources": srcs}
        for tgt, srcs in by_target.items()
        if len(srcs) > 1 and not allow.get(tgt, False)
    ]


def template_match_score(template_columns: list[str], columns: list[str]) -> float:
    """Jaccard overlap of normalized headers (0..1) for template detection."""
    a = {normalize_header(c) for c in template_columns}
    b = {normalize_header(c) for c in columns}
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
