# django-db-snapshots

Copy the data of a Django project from one environment to another - dev,
staging or production to your machine - from the admin, without SSH.

- **Pick what to dump.** Every model, or a selection; the models a selection
  points at come along, so it always loads.
- **One consistent file.** All models are read in one read-only transaction,
  so a dump taken from a live site never refers to rows it does not contain.
  Written as gzipped JSON Lines, which `loaddata` reads one object at a time
  however large the file is.
- **Overwrite, don't pile up.** Dumping to a name that exists replaces it.
  `snapshot_backup` from a nightly schedule keeps exactly one backup file.
- **Replace only what the snapshot holds.** A load empties the models in the
  file and loads them, in one transaction; nothing else in the database is
  touched, and a load that fails changes nothing.
- **Each environment decides.** Servers dump and never load; a developer's
  machine loads. A system check warns if loading is on where `DEBUG` is off.
- **Any storage.** Snapshots go to a Django storage - the filesystem, or S3 and
  friends through django-storages - and download from the admin as a stream.

Dumping and loading run in the background through `django.tasks` (Django 6.0+)
or the [django-tasks](https://pypi.org/project/django-tasks/) backport, or are
queued for `manage.py snapshot_worker`.

## Install

```bash
pip install django-db-snapshots            # Django 6.0+
pip install "django-db-snapshots[tasks]"   # Django 5.2, with background runs
```

```python
INSTALLED_APPS = [..., "db_snapshots"]
```

```bash
python manage.py migrate db_snapshots
```

## Configure each environment

```python
# dev, staging, production: dump, never load
DB_SNAPSHOTS = {"ALLOW_DUMP": True, "ALLOW_LOAD": False}

# your machine: load what you downloaded
DB_SNAPSHOTS = {"ALLOW_LOAD": True, "LOAD_DIRS": [BASE_DIR / "snapshots"]}
```

| Setting | Default | |
|---|---|---|
| `ALLOW_DUMP` | `True` | May this environment write snapshots |
| `ALLOW_LOAD` | `False` | May it replace its data from one |
| `STORAGE` | `"default"` | Alias in `STORAGES` the snapshots live in |
| `LOCATION` | `"db_snapshots/"` | Folder within that storage |
| `BACKUP_NAME` | `"backup.jsonl.gz"` | File `snapshot_backup` overwrites |
| `EXCLUDE` | contenttypes, auth.permission, sessions, admin.logentry, db_snapshots | Never dumped, never emptied |
| `LOAD_DIRS` | `[]` | Local folders a load may read downloaded files from |
| `AFTER_LOAD` | `[]` | Dotted paths of `fn(path, models)` run inside the load, e.g. to scrub e-mails or switch off API keys |
| `RUNNER` | auto | `"tasks"` (django.tasks / django-tasks) or `"worker"` (`snapshot_worker`) |
| `TASK_BACKEND` | `"default"` | Task backend alias for `"tasks"` |
| `DATABASE` | `"default"` | Database alias |

Content types and permissions are never copied: every database creates its
own, and snapshots refer to them by natural key.

## Copying production to your machine

1. In production's admin, **Snapshots > Dump a snapshot** (or wait for the
   nightly `snapshot_backup`), then **download** it.
2. Save the file in one of your `LOAD_DIRS`.
3. In your local admin, **Snapshots > Load a snapshot**, pick it, confirm.

Or from a shell: `manage.py snapshot_dump shop.Order --name orders.jsonl.gz`,
`manage.py snapshot_load --path snapshots/orders.jsonl.gz`.

## Nightly backup

Schedule `python manage.py snapshot_backup` - cron, a systemd timer, Celery
beat, or your task scheduler. It dumps every model to `BACKUP_NAME`, replacing
the previous file only once the new one is complete.

## Know before you load

- **Users are data.** If your models point at `auth.User`, users are dumped
  with them, and a load replaces your local accounts with the source's. Use an
  `AFTER_LOAD` hook to reset passwords or create a local superuser.
- **Loading is row by row.** It uses `loaddata`, which saves one object at a
  time. Millions of rows take a while; the admin shows it running.
- **Large databases need a real task backend.** With Django's default
  `ImmediateBackend`, a run happens inside the request; the admin warns you.
- **Same code on both sides.** Load into a database migrated to the same
  models the snapshot was dumped from.
- **Not a backup tool for disasters.** For point-in-time recovery use your
  database's own tools (`pg_dump`, `mysqldump`, managed backups). This is for
  moving data between environments.

## Development

```bash
python runtests.py
```

## License

BSD-3-Clause.
