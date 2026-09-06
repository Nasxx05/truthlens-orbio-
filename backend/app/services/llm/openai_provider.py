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
from typing import Optional

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

    @property
    def model(self) -> Optional[str]:
        return self._model

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

        if not request.reviews:
            result.error = "no reviews to summarize"
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result

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

        client = openai.AsyncOpenAI(timeout=settings.llm_timeout)

        try:
            if hasattr(client, "responses"):
                result.summary, note = await self._via_responses(client, user_prompt)
            else:
                result.summary, note = await self._via_chat(client, user_prompt)

            if note:
                result.notes.append(note)
            result.ok = result.summary is not None
            if not result.ok and not result.error:
                result.error = note or "model returned no parseable structured output"

        except openai.AuthenticationError:
            result.error = "OPENAI_API_KEY was rejected"
        except openai.PermissionDeniedError:
            result.error = "API key lacks permission for this model"
        except openai.NotFoundError:
            result.error = f"model {self._model!r} not found for this account"
        except openai.RateLimitError:
            result.error = "rate limited by the OpenAI API"
        except openai.BadRequestError as error:
            result.error = f"request rejected: {str(error)[:200]}"
        except openai.APIConnectionError as error:
            result.error = f"could not reach the OpenAI API: {type(error).__name__}"
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
                "openai summary: %s pros, %s cons, confidence=%s, %s reviews in %sms",
                len(result.summary.pros), len(result.summary.cons),
                result.summary.confidence, result.reviews_used, result.duration_ms,
            )
        else:
            logger.warning(
                "llm failure",
                extra={"event_type": "llm_error", "provider": self.name,
                       "model": self._model, "error": result.error},
            )
        return result

    async def _via_responses(self, client, user_prompt: str):
        """Responses API path: ``text_format`` and ``output_parsed``."""
        response = await client.responses.parse(
            model=self._model,
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

    async def _via_chat(self, client, user_prompt: str):
        """Chat Completions path: ``response_format`` and ``message.parsed``."""
        response = await client.chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format=SummaryOutput,
            max_tokens=settings.llm_max_tokens,
        )
        message = response.choices[0].message
        refusal = getattr(message, "refusal", None)
        if refusal:
            return None, f"model declined to answer: {str(refusal)[:160]}"
        return getattr(message, "parsed", None), None
