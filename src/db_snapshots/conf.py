"""Settings, read from ``settings.DB_SNAPSHOTS`` with these defaults.

Each environment decides what it may do. A server typically dumps but never
loads; a developer's machine loads::

    # production / staging / dev server
    DB_SNAPSHOTS = {"ALLOW_DUMP": True, "ALLOW_LOAD": False}

    # local machine
    DB_SNAPSHOTS = {"ALLOW_DUMP": True, "ALLOW_LOAD": True,
                    "LOAD_DIRS": [BASE_DIR / "snapshots"]}
"""

from django.conf import settings

DEFAULTS = {
    # May this environment write snapshots / replace its data from one?
    "ALLOW_DUMP": True,
    "ALLOW_LOAD": False,
    # The storage (an alias in STORAGES) and folder snapshots are kept in.
    # Any Django storage works: the filesystem, S3 through django-storages...
    "STORAGE": "default",
    "LOCATION": "db_snapshots/",
    # The full backup's file name. Writing a snapshot to a name that exists
    # overwrites it, so a nightly `snapshot_backup` keeps exactly one file.
    "BACKUP_NAME": "backup.jsonl.gz",
    # Never dumped, never emptied by a load. Content types and permissions are
    # created by migrate in every database and are referenced by natural key
    # instead; sessions and the admin log belong to one environment.
    "EXCLUDE": [
        "contenttypes",
        "auth.permission",
        "sessions",
        "admin.logentry",
        "db_snapshots",
    ],
    # Extra local folders a load may read from, for a file downloaded from
    # another environment's admin and saved on this machine.
    "LOAD_DIRS": [],
    # Dotted paths of callables run inside the load's transaction after the
    # data is in, as fn(snapshot, models) - to switch off production API keys,
    # scrub e-mail addresses, and so on.
    "AFTER_LOAD": [],
    # "tasks" runs through django.tasks (or the django-tasks backport);
    # "worker" leaves runs queued for `manage.py snapshot_worker`. None picks
    # "tasks" when a task framework is installed.
    "RUNNER": None,
    # The task backend alias when RUNNER is "tasks".
    "TASK_BACKEND": "default",
    "DATABASE": "default",
}


def get(name):
    return getattr(settings, "DB_SNAPSHOTS", {}).get(name, DEFAULTS[name])
