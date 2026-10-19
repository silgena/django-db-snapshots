"""Running a snapshot outside the web request that asked for it.

A multi-gigabyte dump must not run inside a request. With a task framework
installed - ``django.tasks`` on Django 6.0+, or the ``django-tasks`` backport
- a run is enqueued on the configured backend. Without one it stays pending
for ``manage.py snapshot_worker``, which can run from cron or a process
manager.
"""

from __future__ import annotations

import traceback

from django.utils import timezone

from db_snapshots import conf, operations
from db_snapshots.models import Snapshot

try:  # Django 6.0+
    from django.tasks import task, task_backends
except ImportError:  # pragma: no cover - depends on what is installed
    try:
        from django_tasks import task, task_backends
    except ImportError:
        task = task_backends = None

LOG_LIMIT = 50_000


def mode() -> str:
    configured = conf.get("RUNNER")
    if configured:
        return configured
    return "tasks" if task is not None else "worker"


def runs_inline() -> bool:
    """Whether enqueueing runs the snapshot right away, inside the request."""
    if mode() != "tasks":
        return False
    backend = task_backends[conf.get("TASK_BACKEND")]
    return type(backend).__name__ == "ImmediateBackend"


def execute(snapshot_id: int) -> Snapshot:
    """Run one pending snapshot to the end and record how it went."""
    claimed = Snapshot.objects.filter(
        pk=snapshot_id, status=Snapshot.Status.PENDING
    ).update(status=Snapshot.Status.RUNNING, started_at=timezone.now())
    snapshot = Snapshot.objects.get(pk=snapshot_id)
    if not claimed:
        return snapshot
    lines = []

    def log(message):
        lines.append(str(message))

    try:
        if snapshot.kind == Snapshot.Kind.DUMP:
            result = operations.dump(snapshot.name, snapshot.model_labels, log=log)
            snapshot.objects_count = result["objects"]
        else:
            result = operations.load(
                snapshot.name, source_path=snapshot.source_path, log=log
            )
        snapshot.model_labels = result["models"]
        snapshot.size = result["size"]
        snapshot.status = Snapshot.Status.SUCCEEDED
    except Exception as exc:
        snapshot.status = Snapshot.Status.FAILED
        snapshot.error = f"{type(exc).__name__}: {exc}"
        lines.append(traceback.format_exc())
    snapshot.finished_at = timezone.now()
    snapshot.log = "\n".join(lines)[-LOG_LIMIT:]
    snapshot.save()
    return snapshot


if task is not None:

    @task(backend=conf.get("TASK_BACKEND"))
    def run_snapshot(snapshot_id: int) -> None:
        execute(snapshot_id)

else:  # pragma: no cover
    run_snapshot = None


def submit(snapshot: Snapshot) -> None:
    """Hand a pending snapshot to the background runner."""
    if mode() == "tasks":
        if run_snapshot is None:
            raise RuntimeError(
                "DB_SNAPSHOTS['RUNNER'] is 'tasks' but neither django.tasks nor "
                "django-tasks is installed."
            )
        run_snapshot.enqueue(snapshot.pk)
    # In "worker" mode the pending row is the queue.


def run_pending() -> int:
    """Run every pending snapshot, oldest first. For the worker command."""
    done = 0
    for pk in (
        Snapshot.objects.filter(status=Snapshot.Status.PENDING)
        .order_by("created_at")
        .values_list("pk", flat=True)
    ):
        execute(pk)
        done += 1
    return done
