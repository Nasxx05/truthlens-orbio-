"""OpenAI summarization provider.

Exists to prove the provider seam is real: it uses the same prompt and returns
the same ``SummaryResult`` as the Anthropic provider, so switching
``LLM_PROVIDER`` changes the vendor and nothing else. No calling code mentions
either vendor.

Structured output uses the Responses API (``client.responses.parse`` with
``text_format=``), falling back to the Chat Completions helper
(``client.chat.completions.parse`` with ``response_format=``) on SDK versions
that predate it. Both are documented shapes; the fallback exists because which
one is available depends on the installed ``openai`` version.

``openai`` is an optional dependency — the provider reports itself unavailable
when the package or the key is missing rather than failing a request.
"""

import logging
import os
import time
from typing import List, Optional

from app.config import settings
from app.services.llm.base import (
    LLMProvider,
    SummaryOutput,
    SummaryRequest,
    SummaryResult,
)
from app.services.llm.prompt import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    """Summarize reviews with an OpenAI model."""

    name = "openai"

    def __init__(self, model: Optional[str] = None) -> None:
        self._model = model or settings.openai_model
        self._fallback_model = settings.openai_fallback_model

    @property
    def model(self) -> Optional[str]:
        return self._model

    def _candidate_models(self) -> List[str]:
        """Models to try in order: primary, then fallback if it's a different one."""
        candidates = [self._model]
        if self._fallback_model and self._fallback_model != self._model:
            candidates.append(self._fallback_model)
        return candidates

    def available(self) -> bool:
        if not (os.getenv("OPENAI_API_KEY") or "").strip():
            return False
        if not self._model:
            return False
        try:
            import openai  # noqa: F401

            return True
        except ImportError:
            return False

    async def summarize(self, request: SummaryRequest) -> SummaryResult:
        started = time.monotonic()
        result = SummaryResult(provider=self.name, model=self._model)

        try:
            import openai
        except ImportError:
            result.error = "openai package not installed (pip install openai)"
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result

        if not (os.getenv("OPENAI_API_KEY") or "").strip():
            result.error = "OPENAI_API_KEY is not set"
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result

        if not self._model:
            result.error = "OPENAI_MODEL is not set (no default is assumed)"
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result

        user_prompt, used = build_user_prompt(request)
        result.reviews_used = used

        client_kwargs = {"timeout": settings.llm_timeout}
        if settings.openai_base_url:
            client_kwargs["base_url"] = settings.openai_base_url
        client = openai.AsyncOpenAI(**client_kwargs)

        candidates = self._candidate_models()
        try:
            for index, model in enumerate(candidates):
                is_fallback = index > 0
                try:
                    # The Responses API is OpenAI-proprietary; a proxy like
                    # OpenRouter only implements Chat Completions, so a custom
                    # base_url always takes the chat path even though
                    # client.responses still exists.
                    if not settings.openai_base_url and hasattr(client, "responses"):
                        summary, note = await self._via_responses(client, model, user_prompt)
                    else:
                        summary, note = await self._via_chat(client, model, user_prompt)
                except openai.AuthenticationError:
                    summary, note = None, "OPENAI_API_KEY was rejected"
                except openai.PermissionDeniedError:
                    summary, note = None, "API key lacks permission for this model"
                except openai.NotFoundError:
                    summary, note = None, f"model {model!r} not found for this account"
                except openai.RateLimitError:
                    summary, note = None, "rate limited by the OpenAI API"
                except openai.BadRequestError as error:
                    summary, note = None, f"request rejected: {str(error)[:200]}"
                except openai.APIConnectionError as error:
                    summary, note = None, f"could not reach the OpenAI API: {type(error).__name__}"

                if summary is not None:
                    result.summary = summary
                    result.model = model
                    result.ok = True
                    if is_fallback:
                        result.notes.append(f"primary model {candidates[0]!r} failed; used fallback {model!r}")
                    if note:
                        result.notes.append(note)
                    break

                # This attempt produced nothing — record why and, if there is
                # another candidate left, try it instead of giving up.
                result.notes.append(f"{model!r}: {note or 'no parseable structured output'}")
                result.error = note or "model returned no parseable structured output"

        except Exception as error:
            logger.exception("openai summarization failed")
            result.error = f"{type(error).__name__}: {error}"
        finally:
            try:
                await client.close()
            except Exception:
                pass
            result.duration_ms = int((time.monotonic() - started) * 1000)

        if result.ok:
            logger.info(
                "openai summary: %s pros, %s cons, confidence=%s, %s reviews in %sms (model=%s)",
                len(result.summary.pros), len(result.summary.cons),
                result.summary.confidence, result.reviews_used, result.duration_ms, result.model,
            )
        else:
            logger.warning(
                "llm failure",
                extra={"event_type": "llm_error", "provider": self.name,
                       "model": result.model, "error": result.error},
            )
        return result

    async def _via_responses(self, client, model: str, user_prompt: str):
        """Responses API path: ``text_format`` and ``output_parsed``."""
        response = await client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            text_format=SummaryOutput,
            max_output_tokens=settings.llm_max_tokens,
        )

        # A refusal arrives as a content part, not an exception, and carries no
        # schema-conforming JSON — so it must be checked before reading output.
        for output in getattr(response, "output", []) or []:
            if getattr(output, "type", None) != "message":
                continue
            for item in getattr(output, "content", []) or []:
                if getattr(item, "type", None) == "refusal":
                    return None, f"model declined to answer: {getattr(item, 'refusal', '')[:160]}"

        if getattr(response, "status", None) == "incomplete":
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", None) if details else None
            if reason == "max_output_tokens":
                return getattr(response, "output_parsed", None), (
                    "response hit max_output_tokens; summary may be incomplete"
                )

        return getattr(response, "output_parsed", None), None

    async def _via_chat(self, client, model: str, user_prompt: str):
        """Chat Completions path.

        Uses a plain ``json_object`` response format and parses the JSON
        ourselves, rather than the SDK's ``.parse()`` + strict-schema
        ``response_format=SummaryOutput``: many OpenRouter-proxied models
        (including free ones) don't support strict JSON-schema mode, and can
        return a body with ``choices: null`` when asked for it — which
        crashes the SDK's own response parser (``TypeError: 'NoneType'
        object is not iterable``) before we ever see an error. A json_object
        request plus a schema description in the prompt degrades much more
        gracefully across providers.
        """
        schema_hint = (
            "\n\nRespond with a single JSON object only, no prose outside it, matching "
            "exactly this shape: "
            '{"pros": [string], "cons": [string], "verdict": string, '
            '"confidence": "high"|"medium"|"low"|"none", "caveats": [string], '
            '"trust_score": integer 0-100, "star_rating": number 0-5 in steps of 0.5, '
            '"themes": [{"label": string, "sentiment": "positive"|"negative"|"mixed", '
            '"mention_count": integer}], "reasons_to_buy": [string], '
            '"reasons_to_think_twice": [string], "claim_check": string or null}'
        )
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT + schema_hint},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=settings.llm_max_tokens,
        )

        choices = getattr(response, "choices", None) or []
        if not choices:
            note = getattr(response, "error", None)
            return None, f"model returned no choices{f': {note}' if note else ''}"

        message = choices[0].message
        refusal = getattr(message, "refusal", None)
        if refusal:
            return None, f"model declined to answer: {str(refusal)[:160]}"

        content = (getattr(message, "content", None) or "").strip()
        if not content:
            return None, "model returned an empty response"

        try:
            return SummaryOutput.model_validate_json(content), None
        except Exception as error:
            return None, f"model output did not match the expected shape: {error}"
