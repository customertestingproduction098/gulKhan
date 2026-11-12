"""OpenRouter-powered reasoning helpers.

OpenRouter is used to interpret noisy speech transcripts (did the shopkeeper
actually greet the customer?) and can be extended for richer summaries.
When no API key is configured a deterministic local matcher is used.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an analyst for a retail CCTV system. You receive a short speech "
    "transcript captured at the shop counter. Decide whether the shopkeeper "
    "greeted the customer (hello, welcome, good morning, salam, how can I help, "
    "etc.) or simply stayed silent/irrelevant. Reply ONLY with JSON: "
    '{"greeting_detected": boolean, "confidence": number between 0 and 1, '
    '"reason": "short explanation"}.'
)


class OpenRouterService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def available(self) -> bool:
        return self.settings.openrouter_configured

    def classify_greeting(
        self,
        transcript: str,
        person_count: int = 1,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        transcript = (transcript or "").strip()
        if not transcript:
            return {
                "greeting_detected": False,
                "confidence": 0.0,
                "reason": "No speech captured",
                "source": "none",
            }

        if self.available:
            result = self._ask_model(transcript, person_count, labels or [])
            if result:
                return result

        return self._match_locally(transcript)

    def _ask_model(
        self,
        transcript: str,
        person_count: int,
        labels: list[str],
    ) -> dict[str, Any] | None:
        payload = {
            "model": self.settings.openrouter_model,
            "temperature": 0,
            "max_tokens": 120,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Transcript: {transcript!r}\n"
                        f"People visible: {person_count}\n"
                        f"Scene labels: {', '.join(labels) or 'n/a'}"
                    ),
                },
            ],
        }

        try:
            response = requests.post(
                f"{self.settings.openrouter_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": self.settings.openrouter_site_url,
                    "X-Title": "Sentinel AI Surveillance",
                },
                json=payload,
                timeout=15,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            parsed = self._parse_json(content)
            if parsed is None:
                return None

            return {
                "greeting_detected": bool(parsed.get("greeting_detected")),
                "confidence": max(0.0, min(1.0, float(parsed.get("confidence", 0.5)))),
                "reason": str(parsed.get("reason", ""))[:300],
                "source": "openrouter",
            }
        except (requests.RequestException, KeyError, ValueError, TypeError) as error:
            logger.warning("OpenRouter greeting classification failed: %s", error)
            return None

    @staticmethod
    def _parse_json(content: str | None) -> dict[str, Any] | None:
        if not content:
            return None
        cleaned = content.strip()
        cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.MULTILINE).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not match:
                return None
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None

    def _match_locally(self, transcript: str) -> dict[str, Any]:
        normalized = re.sub(r"\s+", " ", transcript.lower()).strip()
        for phrase in self.settings.greeting_phrases:
            phrase = phrase.strip().lower()
            if not phrase:
                continue
            if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", normalized):
                return {
                    "greeting_detected": True,
                    "confidence": 0.78,
                    "reason": f"Matched greeting phrase '{phrase}'",
                    "source": "local-matcher",
                }

        return {
            "greeting_detected": False,
            "confidence": 0.35,
            "reason": "No greeting phrase found in transcript",
            "source": "local-matcher",
        }


_service: OpenRouterService | None = None


def get_openrouter_service() -> OpenRouterService:
    global _service
    if _service is None:
        _service = OpenRouterService()
    return _service
