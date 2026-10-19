"""Dumping to and loading from the snapshots storage.

Both read and write through Django's own commands - ``dumpdata`` and
``loaddata`` - in the JSON Lines format, gzipped. JSON Lines is read one
object per line, so ``loaddata`` holds one object in memory at a time however
large the file, and both commands stream gzip themselves.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from django.apps import apps
from django.core.files import File
from django.core.files.storage import storages
from django.core.management import CommandError, call_command
from django.core.management.color import no_style
from django.db import connections, transaction
from django.utils.module_loading import import_string

from db_snapshots import conf, selection

SUFFIX = ".jsonl.gz"
_MODEL_AT_LINE_START = re.compile(r'^\{"model": "([^"]+)"')
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def storage():
    return storages[conf.get("STORAGE")]


def storage_path(name: str) -> str:
    return conf.get("LOCATION") + name


def check_name(name: str) -> str:
    """A bare file name ending in .jsonl.gz, or ValueError."""
    if not _SAFE_NAME.match(name or "") or not name.endswith(SUFFIX):
        raise ValueError(
            f"{name!r} is not a snapshot name (letters, digits, '._-', ending {SUFFIX})."
        )
    return name


def stored_files() -> list[dict]:
    """Snapshot files in the storage, newest first."""
    store, location = storage(), conf.get("LOCATION")
    try:
        _dirs, files = store.listdir(location)
    except (FileNotFoundError, NotImplementedError):
        return []
    found = []
    for name in files:
        if not name.endswith(SUFFIX):
            continue
        path = location + name
        try:
            modified = store.get_modified_time(path)
        except (NotImplementedError, OSError):
            modified = None
        found.append({"name": name, "size": store.size(path), "modified": modified})
    return sorted(found, key=lambda item: item["modified"] or 0, reverse=True)


def local_files() -> list[dict]:
    """Snapshot files in LOAD_DIRS, for loading a downloaded file."""
    found = []
    for folder in conf.get("LOAD_DIRS"):
        folder = Path(folder)
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.is_file() and path.name.endswith(SUFFIX):
                info = path.stat()
                found.append(
                    {
                        "name": path.name,
                        "path": str(path),
                        "size": info.st_size,
                        "modified": info.st_mtime,
                    }
                )
    return sorted(found, key=lambda item: item["modified"], reverse=True)


def resolve_local(path: str) -> Path:
    """A snapshot file inside one of LOAD_DIRS, or ValueError."""
    candidate = Path(path).resolve()
    for folder in conf.get("LOAD_DIRS"):
        if candidate.parent == Path(folder).resolve() and candidate.name.endswith(
            SUFFIX
        ):
            if candidate.is_file():
                return candidate
    raise ValueError(f"{path} is not a snapshot file in LOAD_DIRS.")


# ---------------------------------------------------------------------------
# Reading consistently
# ---------------------------------------------------------------------------
_SNAPSHOT_SQL = {
    "postgresql": "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
    "mysql": "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
    "oracle": "SET TRANSACTION READ ONLY",
}


@contextlib.contextmanager
def read_only_snapshot(using: str):
    """A transaction in which every query sees the database at one instant.

    Uses ``connection.ops.read_only_snapshot_sql()`` where Django has it, and
    the same statements otherwise. SQLite needs none: a transaction there
    already reads one snapshot. Inside a transaction that is already open the
    caller's transaction applies, since the statement must come first.
    """
    connection = connections[using]
    ops = connection.ops
    sql = (
        ops.read_only_snapshot_sql()
        if hasattr(ops, "read_only_snapshot_sql")
        else _SNAPSHOT_SQL.get(connection.vendor)
    )
    if connection.in_atomic_block:
        sql = None
    with transaction.atomic(using=using, savepoint=False):
        if sql:
            with connection.cursor() as cursor:
                cursor.execute(sql)
        yield


# ---------------------------------------------------------------------------
# Dump
# ---------------------------------------------------------------------------
class _Counter(io.TextIOBase):
    """Counts the lines (objects) dumpdata writes into the gzip stream."""

    def __init__(self, stream):
        self._stream = stream
        self.lines = 0

    def write(self, text):
        self.lines += text.count("\n")
        return self._stream.write(text)

    def isatty(self):
        return False


def dump(name: str, model_labels, *, log=print) -> dict:
    """Write the models to ``name`` in the storage, overwriting it. Return stats."""
    check_name(name)
    using = conf.get("DATABASE")
    models = selection.resolve(model_labels)
    labels = selection.labels(models)
    log(f"Dumping {len(models)} model(s) to {storage_path(name)}")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="db-snapshot-") as folder:
        path = Path(folder) / name
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            counter = _Counter(stream)
            with read_only_snapshot(using):
                call_command(
                    "dumpdata",
                    *labels,
                    format="jsonl",
                    use_natural_foreign_keys=True,
                    database=using,
                    stdout=counter,
                    verbosity=0,
                )
        size = path.stat().st_size
        log(f"Wrote {counter.lines} object(s), {size} bytes compressed; uploading")
        store, target = storage(), storage_path(name)
        # Delete first: Storage.save() would otherwise pick another name
        # rather than overwrite. A failed dump never gets this far, so the
        # previous file is only replaced by a complete one.
        if store.exists(target):
            store.delete(target)
        with open(path, "rb") as handle:
            saved = store.save(target, File(handle, name=name))
    if saved != target:
        raise CommandError(f"The storage saved the snapshot as {saved}, not {target}.")
    log(f"Done in {time.monotonic() - started:.0f}s")
    return {"models": labels, "size": size, "objects": counter.lines}


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
def models_in(path: Path) -> list:
    """The models a snapshot file contains, read from its lines."""
    labels = set()
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            match = _MODEL_AT_LINE_START.match(line)
            if match:
                labels.add(match.group(1))
    found = []
    for label in sorted(labels):
        try:
            found.append(apps.get_model(label))
        except LookupError:
            raise CommandError(
                f"The snapshot contains {label}, which this project does not have."
            )
    return found


def _tables(models) -> list[str]:
    """The models' tables and their auto-created many-to-many tables."""
    tables = []
    for model in models:
        tables.append(model._meta.db_table)
        for field in model._meta.local_many_to_many:
            through = field.remote_field.through
            if through._meta.auto_created:
                tables.append(through._meta.db_table)
    return sorted(set(tables))


@contextlib.contextmanager
def _local_copy(name: str = "", source_path: str = ""):
    """The snapshot as a local file, copied out of the storage if need be."""
    if source_path:
        yield resolve_local(source_path)
        return
    check_name(name)
    with tempfile.TemporaryDirectory(prefix="db-snapshot-") as folder:
        path = Path(folder) / name
        with storage().open(storage_path(name), "rb") as source, open(
            path, "wb"
        ) as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        yield path


def load(name: str = "", *, source_path: str = "", log=print) -> dict:
    """Replace the data of the models in the snapshot with the snapshot's.

    Only the models the file contains are emptied - nothing else in the
    database is touched - and emptying and loading are one transaction, so a
    load that fails anywhere leaves the data as it was.
    """
    if not conf.get("ALLOW_LOAD"):
        raise CommandError(
            "Loading is not allowed in this environment (DB_SNAPSHOTS['ALLOW_LOAD'])."
        )
    using = conf.get("DATABASE")
    connection = connections[using]
    with _local_copy(name, source_path) as path:
        models = models_in(path)
        excluded = [
            model._meta.label for model in models if selection.is_excluded(model)
        ]
        if excluded:
            raise CommandError(
                "The snapshot contains excluded models: " + ", ".join(excluded)
            )
        tables = _tables(models)
        log(f"Replacing {len(models)} model(s) ({len(tables)} tables) from {path.name}")
        started = time.monotonic()
        out = io.StringIO()
        with transaction.atomic(using=using):
            statements = connection.ops.sql_flush(
                no_style(), tables, reset_sequences=False, allow_cascade=False
            )
            with connection.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)
            call_command("loaddata", str(path), database=using, stdout=out, verbosity=1)
            for hook in conf.get("AFTER_LOAD"):
                import_string(hook)(path, models)
    log(out.getvalue().strip())
    log(f"Done in {time.monotonic() - started:.0f}s")
    return {
        "models": selection.labels(models),
        "size": os.path.getsize(path) if path.exists() else None,
    }
