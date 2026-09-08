"""Anthropic (Claude) summarization provider.

Uses the official ``anthropic`` SDK and its structured-output support:
``messages.parse(output_format=SummaryOutput)`` validates the response against
the pydantic schema at the API boundary, so the route never has to parse pros
and cons out of prose or defend against malformed JSON.

Cost notes, since the operator has to choose a model:

  * The system prompt is stable across every request, so it is marked
    cacheable. Cache reads are far cheaper than fresh input tokens. Whether it
    actually caches depends on the model's minimum cacheable prefix, so
    ``cache_read_input_tokens`` is reported back rather than assumed.
  * Reasoning depth is configurable (``LLM_EFFORT``). Summarizing reviews is a
    routine extraction task and lower effort is usually enough; the default is
    left at the API default so that cost/quality remains an explicit decision.
"""

import logging
import os
import time
from typing import Optional

from pydantic import ValidationError

from app.config import settings
from app.services.llm.base import (
    LLMProvider,
    SummaryOutput,
    SummaryRequest,
    SummaryResult,
)
from app.services.llm.prompt import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    """Summarize reviews with Claude."""

    name = "anthropic"

    def __init__(self, model: Optional[str] = None) -> None:
        self._model = model or settings.anthropic_model

    @property
    def model(self) -> Optional[str]:
        return self._model

    def available(self) -> bool:
        """Credentials present?

        Read at call time rather than cached, so adding a key to the
        environment does not require a restart. An unset ANTHROPIC_API_KEY does
        not by itself mean there are no credentials — the SDK also resolves
        ANTHROPIC_AUTH_TOKEN and an `ant auth login` profile — but requiring an
        explicit key here keeps a server's behaviour predictable rather than
        depending on whoever's shell it inherited.
        """
        return bool((os.getenv("ANTHROPIC_API_KEY") or "").strip())

    async def summarize(self, request: SummaryRequest) -> SummaryResult:
        started = time.monotonic()
        result = SummaryResult(provider=self.name, model=self._model)

        try:
            import anthropic
        except ImportError:
            result.error = "anthropic package not installed (pip install anthropic)"
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result

        if not self.available():
            result.error = "ANTHROPIC_API_KEY is not set"
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result

        user_prompt, used = build_user_prompt(request)
        result.reviews_used = used

        client = anthropic.AsyncAnthropic(timeout=settings.llm_timeout)

        # Effort is only sent when configured, so an unset value means the API
        # default rather than this code silently choosing one.
        output_config = {"effort": settings.llm_effort} if settings.llm_effort else None

        # One retry, specifically for a malformed/off-schema response — never
        # for auth/rate-limit/network failures, which a retry cannot fix. The
        # retry repeats the exact same request with one added instruction, so
        # a model that ignored the schema once is told again, explicitly.
        REPAIR_HINT = (
            "\n\nYour previous response did not match the required JSON schema. "
            "Return only valid JSON matching the schema, with no extra text."
        )
        attempt = 0
        max_attempts = 2

        try:
            while True:
                attempt += 1
                prompt = user_prompt if attempt == 1 else user_prompt + REPAIR_HINT
                kwargs = dict(
                    model=self._model,
                    max_tokens=settings.llm_max_tokens,
                    # The system prompt never varies, which makes it the one part of
                    # the request worth caching across products.
                    system=[
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": prompt}],
                    output_format=SummaryOutput,
                )
                if output_config:
                    kwargs["output_config"] = output_config

                try:
                    response = await client.messages.parse(**kwargs)
                    break
                except ValidationError as error:
                    if attempt >= max_attempts:
                        raise
                    logger.info(
                        "model output failed schema validation on attempt %s/%s; retrying with repair hint",
                        attempt, max_attempts,
                    )
                    result.notes.append(
                        f"first response did not match schema ({len(error.errors())} error(s)); retried once"
                    )
                    continue

            # A safety refusal arrives as HTTP 200 with stop_reason "refusal",
            # so checking status alone would read an empty response as success.
            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                result.error = f"model declined to answer ({category or 'refusal'})"
                return result

            if stop_reason == "max_tokens":
                # The JSON is likely truncated and parse() may still have
                # produced a partial object; say so rather than presenting it
                # as a complete verdict.
                result.notes.append(
                    f"response hit max_tokens ({settings.llm_max_tokens}); summary may be incomplete"
                )

            parsed = getattr(response, "parsed_output", None)
            if parsed is None:
                result.error = "model returned no parseable structured output"
                return result

            result.summary = parsed
            result.ok = True

            usage = getattr(response, "usage", None)
            if usage is not None:
                result.input_tokens = getattr(usage, "input_tokens", None)
                result.output_tokens = getattr(usage, "output_tokens", None)
                result.cached_tokens = getattr(usage, "cache_read_input_tokens", None)

        except anthropic.AuthenticationError:
            result.error = "ANTHROPIC_API_KEY was rejected"
        except anthropic.PermissionDeniedError:
            result.error = "API key lacks permission for this model"
        except anthropic.NotFoundError:
            result.error = f"model {self._model!r} not found for this account"
        except anthropic.RateLimitError as error:
            retry_after = None
            try:
                retry_after = error.response.headers.get("retry-after")
            except Exception:
                pass
            result.error = "rate limited by the Anthropic API" + (
                f"; retry after {retry_after}s" if retry_after else ""
            )
        except anthropic.BadRequestError as error:
            result.error = f"request rejected: {getattr(error, 'message', str(error))[:200]}"
        except anthropic.APIStatusError as error:
            result.error = f"API error {error.status_code}: {getattr(error, 'message', '')[:160]}"
        except anthropic.APIConnectionError as error:
            result.error = f"could not reach the Anthropic API: {type(error).__name__}"
        except ValidationError as error:
            # The model returned something that is not the requested schema —
            # prose instead of JSON, or a missing field. Report it as a short
            # sentence rather than dumping pydantic's multi-line diagnostic
            # into an API response, and log it at info: it is a model
            # behaviour, not a bug in this service.
            count = len(error.errors())
            logger.info(
                "model output failed schema validation (%s error(s)) after %s attempt(s)",
                count, attempt,
            )
            result.error = (
                f"model output did not match the requested schema after {attempt} attempt(s) "
                f"({count} validation error(s))"
            )
        except Exception as error:
            # Never let summarization take down a request that already has
            # reviews and videos to return.
            logger.exception("anthropic summarization failed")
            result.error = f"{type(error).__name__}: {error}"
        finally:
            try:
                await client.close()
            except Exception:
                pass
            result.duration_ms = int((time.monotonic() - started) * 1000)

        if result.ok:
            logger.info(
                "anthropic summary: %s pros, %s cons, confidence=%s, %s reviews, "
                "%s in / %s out tokens (%s cached) in %sms",
                len(result.summary.pros), len(result.summary.cons),
                result.summary.confidence, result.reviews_used,
                result.input_tokens, result.output_tokens, result.cached_tokens,
                result.duration_ms,
            )
        else:
            logger.warning(
                "llm failure",
                extra={"event_type": "llm_error", "provider": self.name,
                       "model": self._model, "error": result.error},
            )

        return result
