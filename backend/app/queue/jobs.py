"""The analysis job queue.

Scraping is slow and bursty. Running it inside the request that asked for it
means a handful of shoppers can saturate the process, and every new request
waits behind their scrapes. So a request enqueues a job and follows its event
stream; workers do the actual work, with their own concurrency limit.

Redis primitives and why:

``LPUSH``/``BRPOP`` on ``trustlens:queue``
    The job queue itself. Blocking pop means an idle worker costs nothing.

Redis **Streams** (``XADD``/``XREAD``) for events
    Not pub/sub. A subscriber that connects after the worker has begun
    publishing would silently miss the early events, and "reviews arrived
    before you were listening" is indistinguishable from "there were no
    reviews". A stream can be read from the beginning and then followed, so a
    late reader still sees everything.

``SET NX`` for in-flight deduplication
    Two shoppers on the same product should not cause two scrapes. The second
    request attaches to the first job's stream instead.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from app.config import settings
from app.queue.broker import broker

logger = logging.getLogger(__name__)

QUEUE_KEY = "trustlens:queue"
STREAM_KEY = "trustlens:events:{job_id}"
INFLIGHT_KEY = "trustlens:inflight:{cache_key}"
JOB_KEY = "trustlens:job:{job_id}"

# Marks the end of a job's event stream, so a reader knows to stop following.
TERMINAL_EVENTS = {"done", "error", "cancelled"}


@dataclass
class Job:
    """One analysis request."""

    job_id: str
    product_url: Optional[str] = None
    product_name: Optional[str] = None
    product_id: Optional[str] = None
    site_hint: Optional[str] = None
    cache_key: Optional[str] = None
    enqueued_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id": self.job_id,
                "product_url": self.product_url,
                "product_name": self.product_name,
                "product_id": self.product_id,
                "site_hint": self.site_hint,
                "cache_key": self.cache_key,
                "enqueued_at": self.enqueued_at,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "Job":
        data = json.loads(raw)
        return cls(**data)


def new_job_id() -> str:
    return uuid.uuid4().hex[:16]


async def enqueue(job: Job) -> Optional[str]:
    """Queue a job, or attach to an identical one already running.

    Returns the job id whose stream the caller should follow, or ``None`` if
    the queue is unavailable and the caller must run the analysis itself.
    """
    client = await broker.client()
    if client is None:
        return None

    try:
        # Deduplicate by product, not by request. The lock's TTL is the ceiling
        # on how long a crashed worker can block a product from being retried.
        if job.cache_key:
            inflight = INFLIGHT_KEY.format(cache_key=job.cache_key)
            # `ex` must be an int: redis-py raises DataError on a float, and
            # job_timeout is configured as a float.
            claimed = await client.set(
                inflight, job.job_id, nx=True, ex=int(settings.job_timeout) + 30
            )
            if not claimed:
                existing = await client.get(inflight)
                if existing:
                    logger.info(
                        "job for %s already running as %s; attaching",
                        job.cache_key[:8], existing[:8],
                    )
                    return existing

        await client.hset(
            JOB_KEY.format(job_id=job.job_id),
            mapping={"state": "queued", "enqueued_at": str(job.enqueued_at)},
        )
        await client.expire(JOB_KEY.format(job_id=job.job_id), settings.job_result_ttl)
        await client.lpush(QUEUE_KEY, job.to_json())

        depth = await client.llen(QUEUE_KEY)
        logger.info("enqueued job %s (queue depth %s)", job.job_id[:8], depth)
        return job.job_id
    except Exception as error:
        logger.warning("enqueue failed (%s); running inline", type(error).__name__)
        return None


async def publish(job_id: str, event: dict) -> None:
    """Append one event to a job's stream."""
    if not job_id:
        return
    client = await broker.client()
    if client is None:
        return
    try:
        stream = STREAM_KEY.format(job_id=job_id)
        await client.xadd(
            stream,
            {"data": json.dumps(event, separators=(",", ":"), default=str)},
            maxlen=settings.job_stream_maxlen,
            approximate=True,
        )
        # The stream outlives the job briefly so a reconnecting reader can
        # still collect the tail.
        await client.expire(stream, settings.job_result_ttl)
    except Exception as error:
        logger.debug("event publish failed: %s", error)


async def follow(job_id: str, *, timeout: Optional[float] = None) -> AsyncIterator[dict]:
    """Yield a job's events, from the beginning, until it terminates.

    Reading from id ``0`` first means every event already published is
    delivered before the reader starts following new ones — the reason this
    uses a stream rather than pub/sub.
    """
    if not job_id:
        # Nothing to follow. Callers should run the analysis inline instead;
        # returning quietly is safer than raising inside a generator.
        return

    client = await broker.client()
    if client is None:
        return

    stream = STREAM_KEY.format(job_id=job_id)
    deadline = time.monotonic() + (timeout or settings.job_timeout)
    last_id = "0"

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        block_ms = max(200, min(2000, int(remaining * 1000)))

        try:
            response = await client.xread({stream: last_id}, count=50, block=block_ms)
        except Exception as error:
            logger.warning("stream read failed for %s: %s", job_id[:8], type(error).__name__)
            return

        if not response:
            continue

        for _name, entries in response:
            for entry_id, fields in entries:
                last_id = entry_id
                raw = fields.get("data")
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                yield event
                if event.get("event") in TERMINAL_EVENTS:
                    return

    # Timed out with no terminal event: the worker died, or the job outran its
    # budget. Say so rather than leaving the client hanging.
    logger.warning("job %s produced no terminal event before timeout", job_id[:8])
    yield {
        "event": "error",
        "message": (
            f"analysis did not complete within {timeout or settings.job_timeout:.0f}s "
            "(worker may be unavailable)"
        ),
    }


async def release(cache_key: Optional[str], job_id: str) -> None:
    """Release the in-flight lock, if this job owns it."""
    if not cache_key:
        return
    client = await broker.client()
    if client is None:
        return
    try:
        inflight = INFLIGHT_KEY.format(cache_key=cache_key)
        # Only the owner clears the lock: a slow job must not release a lock
        # that a newer job has since taken.
        if await client.get(inflight) == job_id:
            await client.delete(inflight)
    except Exception:
        pass


async def mark(job_id: str, state: str, **extra) -> None:
    """Record job state, for the queue-status endpoint."""
    if not job_id:
        return
    client = await broker.client()
    if client is None:
        return
    try:
        mapping = {"state": state, "updated_at": str(time.time())}
        mapping.update({key: str(value) for key, value in extra.items()})
        await client.hset(JOB_KEY.format(job_id=job_id), mapping=mapping)
        await client.expire(JOB_KEY.format(job_id=job_id), settings.job_result_ttl)
    except Exception:
        pass


async def claim(timeout: int = 5) -> Optional[Job]:
    """Block until a job is available, or return ``None`` on timeout."""
    client = await broker.client()
    if client is None:
        return None
    try:
        popped = await client.brpop(QUEUE_KEY, timeout=timeout)
    except Exception as error:
        logger.debug("claim failed: %s", error)
        return None
    if not popped:
        return None
    _key, raw = popped
    try:
        return Job.from_json(raw)
    except Exception:
        logger.warning("discarding unreadable job payload")
        return None


async def queue_stats() -> dict:
    """Depth and worker liveness, for the health endpoint."""
    client = await broker.client()
    if client is None:
        return {"available": False}
    try:
        depth = await client.llen(QUEUE_KEY)
        workers = await client.scard("trustlens:workers")
        return {"available": True, "depth": int(depth), "workers": int(workers)}
    except Exception:
        return {"available": False}


async def heartbeat(worker_id: str, ttl: int = 30) -> None:
    """Announce a live worker, so the API can report whether any exist."""
    client = await broker.client()
    if client is None:
        return
    try:
        await client.sadd("trustlens:workers", worker_id)
        await client.expire("trustlens:workers", ttl)
    except Exception:
        pass
