"""The analysis worker.

Run alongside the API:

    python -m app.worker

Claims jobs from Redis, runs the pipeline, publishes each stage to the job's
event stream, and writes the finished result to the cache. Several workers can
run at once; each limits its own concurrency so a burst queues rather than
turning into a hundred simultaneous scrapes.

The API process does no scraping while a worker is running, which is the point:
a slow retailer no longer competes with request handling.
"""

import asyncio
import logging
import os
import signal
import socket
import time
import uuid

from app.config import settings
from app.db import cache
from app.logging_config import configure_logging
from app.queue import broker, claim, jobs
from app.services.pipeline import analyze_stream

logger = logging.getLogger("app.worker")

_shutdown = asyncio.Event()


async def run_job(job: jobs.Job, semaphore: asyncio.Semaphore) -> None:
    """Execute one job, streaming its events and caching the result."""
    async with semaphore:
        started = time.monotonic()
        await jobs.mark(job.job_id, "running")
        logger.info(
            "job started", extra={"job_id": job.job_id[:8], "product": job.product_name}
        )

        assembled = {}
        try:
            async for event in analyze_stream(
                product_url=job.product_url,
                product_name=job.product_name,
                product_id=job.product_id,
                site_hint=job.site_hint,
            ):
                await jobs.publish(job.job_id, event)

                name = event.get("event")
                if name == "reviews":
                    assembled["reviews"] = event["reviews"]
                    assembled["reviews_passed"] = event["reviews_passed"]
                    assembled["filter_report"] = event["filter_report"]
                    assembled["sources"] = event["sources"]
                elif name == "summary":
                    assembled["summary"] = event["summary"]
                    assembled["llm"] = event["llm"]
                elif name == "match":
                    assembled["product_match"] = event["product_match"]
                elif name == "done":
                    assembled["status"] = event["status"]
                    assembled["message"] = event["message"]
                    assembled["contributed"] = event["contributed"]
                    assembled["video_sources"] = event["video_sources"]
                    assembled["videos"] = event["videos"]

            if job.cache_key and assembled.get("status"):
                await cache.put(
                    job.cache_key,
                    assembled,
                    status=assembled["status"],
                    product_id=job.product_id,
                    site=job.site_hint,
                    product_name=job.product_name,
                    canonical_url=job.product_url,
                    review_count=assembled.get("reviews_passed", 0),
                )

            await jobs.mark(job.job_id, "done", status=assembled.get("status", "unknown"))
            logger.info(
                "job finished",
                extra={
                    "job_id": job.job_id[:8],
                    "status": assembled.get("status"),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
        except Exception as error:
            logger.exception("job failed", extra={"job_id": job.job_id[:8]})
            await jobs.publish(
                job.job_id, {"event": "error", "message": f"{type(error).__name__}: {error}"}
            )
            await jobs.mark(job.job_id, "failed", error=type(error).__name__)
        finally:
            # Release the product lock whatever happened, or that product is
            # unanalysable until the lock's TTL expires.
            await jobs.release(job.cache_key, job.job_id)


async def main() -> None:
    configure_logging()
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:4]}"

    if not await broker.available():
        logger.error(
            "redis is not reachable at %r — the worker has nothing to consume. "
            "Set REDIS_URL and start Redis.",
            settings.redis_url or "(unset)",
        )
        return

    await cache.connect()

    semaphore = asyncio.Semaphore(settings.worker_concurrency)
    running: set = set()

    logger.info(
        "worker ready", extra={"worker_id": worker_id, "concurrency": settings.worker_concurrency}
    )

    last_beat = 0.0
    while not _shutdown.is_set():
        now = time.monotonic()
        if now - last_beat > 10:
            await jobs.heartbeat(worker_id)
            last_beat = now

        job = await claim(timeout=2)
        if job is None:
            # Drop finished tasks so the set does not grow unbounded.
            running = {task for task in running if not task.done()}
            continue

        task = asyncio.create_task(run_job(job, semaphore))
        running.add(task)
        task.add_done_callback(running.discard)

    if running:
        logger.info("draining %s in-flight job(s)", len(running))
        await asyncio.gather(*running, return_exceptions=True)

    await cache.close()
    await broker.close()
    logger.info("worker stopped", extra={"worker_id": worker_id})


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Finish in-flight jobs on SIGTERM rather than dropping them."""
    for signal_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, signal_name), _shutdown.set)
        except (NotImplementedError, AttributeError):  # pragma: no cover - platform dependent
            pass


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
