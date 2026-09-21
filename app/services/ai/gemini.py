"""Google Gemini provider (REST via httpx, no SDK dependency).

Only ambiguous columns reach this code. Payload per column: the header, up to six
masked sample values, and the list of free target fields with descriptions.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from app.services.ai.base import AISuggestion, masked_samples

log = logging.getLogger("magic_import.ai")

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_PROMPT = """You map spreadsheet columns to fields of a target import schema.

Target fields (name: type - description):
{targets}

Source columns with a few masked sample values:
{sources}

For every source column choose the single best target field name, or null if none fits.
Answer ONLY with JSON of the form:
{{"mappings": [{{"source": "<column>", "target": "<field or null>", "confidence": "high|low",
"reason": "<short>"}}]}}
"""


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", timeout: float = 20.0):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------ prompt
    def build_prompt(self, sources, targets) -> str:
        t_lines = [f"- {t.name}: {t.type} - {t.description or t.display_name}" for t in targets]
        s_lines = [
            f"- {s.name!r}: samples {json.dumps(masked_samples(s.samples), ensure_ascii=False)}"
            for s in sources
        ]
        return _PROMPT.format(targets="\n".join(t_lines), sources="\n".join(s_lines))

    # ------------------------------------------------------------------ call
    def suggest(self, sources, targets) -> dict[str, AISuggestion]:
        if not self.available or not sources or not targets:
            return {}
        prompt = self.build_prompt(sources, targets)
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }
        url = _ENDPOINT.format(model=self.model)
        try:
            resp = httpx.post(
                url,
                params={"key": self.api_key},
                json=body,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            log.warning("Gemini request failed: %s", type(exc).__name__)
            return {}
        except ValueError:
            log.warning("Gemini returned a non-JSON response")
            return {}
        return self.parse_response(data, {t.name for t in targets})

    @staticmethod
    def parse_response(data: dict, allowed_targets: set[str]) -> dict[str, AISuggestion]:
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            return {}
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {}
        out: dict[str, AISuggestion] = {}
        for item in payload.get("mappings", []) or []:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            target = item.get("target")
            if not source:
                continue
            if target not in allowed_targets:
                target = None
            out[str(source)] = AISuggestion(
                target_field=target,
                confidence="high" if item.get("confidence") == "high" else "low",
                reason=str(item.get("reason") or "")[:200],
            )
        return out


def build_provider(api_key: str, model: str):
    from app.services.ai.base import NullProvider

    if api_key:
        return GeminiProvider(api_key=api_key, model=model)
    return NullProvider()
