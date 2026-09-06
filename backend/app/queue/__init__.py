"""Background job execution.

    broker.py  Redis connection, with graceful absence
    jobs.py    enqueue / claim / publish / follow, and in-flight deduplication

Optional by design: with no Redis the API runs analyses inline exactly as
before, and says so.
"""

from app.queue.broker import broker
from app.queue.jobs import Job, claim, enqueue, follow, mark, new_job_id, publish, queue_stats, release

__all__ = [
    "broker", "Job", "claim", "enqueue", "follow", "mark",
    "new_job_id", "publish", "queue_stats", "release",
]
