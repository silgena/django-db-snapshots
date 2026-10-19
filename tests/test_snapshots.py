import gzip
import io
import json
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.checks import Warning as CheckWarning
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from db_snapshots import checks, operations, runner, selection
from db_snapshots.models import Snapshot
from tests.testapp.models import Customer, Note, Order, Tag


def snapshots(**extra):
    return override_settings(
        DB_SNAPSHOTS={
            "ALLOW_DUMP": True,
            "ALLOW_LOAD": True,
            "RUNNER": "worker",
            **extra,
        }
    )


def seed():
    ada = Customer.objects.create(name="Ada")
    order = Order.objects.create(customer=ada, total=Decimal("12.50"))
    order.tags.add(Tag.objects.create(name="priority"))
    Note.objects.create(text="kept out of order dumps")
    return order


def lines(name):
    path = operations.storage().path(operations.storage_path(name))
    with gzip.open(path, "rt") as handle:
        return [json.loads(line) for line in handle]


def models_in_file(name):
    return {line["model"] for line in lines(name)}


class SelectionTests(TestCase):
    def test_a_selection_brings_what_it_points_at(self):
        chosen = selection.with_dependencies(selection.resolve(["testapp.Order"]))
        labels = set(selection.labels(chosen))

        self.assertTrue(
            {"testapp.Order", "testapp.Customer", "testapp.Tag", "auth.User"} <= labels
        )
        self.assertNotIn("testapp.Note", labels)
        # Created by migrate everywhere; referenced by natural key instead.
        self.assertNotIn("auth.Permission", labels)
        self.assertNotIn("contenttypes.ContentType", labels)

    def test_excluded_and_unknown_models_cannot_be_chosen(self):
        for label in ("sessions.Session", "db_snapshots.Snapshot", "testapp.Nope"):
            with self.subTest(label=label), self.assertRaises(LookupError):
                selection.resolve([label])


@snapshots()
class DumpTests(TestCase):
    def test_a_dump_holds_the_selection_and_its_dependencies(self):
        seed()
        call_command(
            "snapshot_dump",
            "testapp.Order",
            "--name",
            "orders.jsonl.gz",
            stdout=io.StringIO(),
        )

        found = models_in_file("orders.jsonl.gz")
        self.assertIn("testapp.order", found)
        self.assertIn("testapp.customer", found)
        self.assertNotIn("testapp.note", found)
        self.assertNotIn("contenttypes.contenttype", found)

    def test_dumping_to_a_name_again_overwrites_it(self):
        seed()
        call_command("snapshot_backup", stdout=io.StringIO())
        Customer.objects.create(name="Grace")
        call_command("snapshot_backup", stdout=io.StringIO())

        # Storage.save() keeps both as backup_<random>.jsonl.gz unless the old
        # file is deleted first.
        _dirs, files = operations.storage().listdir("db_snapshots/")
        self.assertEqual(
            [name for name in files if name.startswith("backup")], ["backup.jsonl.gz"]
        )
        names = [
            line["fields"]["name"]
            for line in lines("backup.jsonl.gz")
            if line["model"] == "testapp.customer"
        ]
        self.assertEqual(sorted(names), ["Ada", "Grace"])

    def test_a_dump_is_refused_where_dumping_is_off(self):
        with snapshots(ALLOW_DUMP=False), self.assertRaisesMessage(
            CommandError, "not allowed"
        ):
            call_command("snapshot_backup", stdout=io.StringIO())

    def test_names_cannot_leave_the_snapshots_folder(self):
        for name in ("../escape.jsonl.gz", "a/b.jsonl.gz", "backup.json"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                operations.check_name(name)


class ConsistencyTests(TransactionTestCase):
    available_apps = [
        "db_snapshots",
        "tests.testapp",
        "django.contrib.auth",
        "django.contrib.contenttypes",
    ]

    @snapshots()
    def test_dumpdata_runs_inside_one_transaction(self):
        from django.core.management.commands import dumpdata

        seen = []
        original = dumpdata.Command.handle

        def handle(command, *args, **options):
            seen.append(connection.in_atomic_block)
            return original(command, *args, **options)

        with mock.patch.object(
            dumpdata.Command, "handle", autospec=True, side_effect=handle
        ):
            operations.dump("x.jsonl.gz", ["testapp.Note"], log=lambda message: None)
        self.assertEqual(seen, [True])
        self.assertFalse(connection.in_atomic_block)


@snapshots()
class LoadTests(TestCase):
    def setUp(self):
        self.downloads = Path(tempfile.mkdtemp(prefix="snapshot-downloads-"))
        self.addCleanup(shutil.rmtree, self.downloads, True)

    def test_a_load_replaces_only_the_models_in_the_snapshot(self):
        seed()
        call_command(
            "snapshot_dump",
            "testapp.Order",
            "--name",
            "orders.jsonl.gz",
            stdout=io.StringIO(),
        )
        Order.objects.all().delete()
        Customer.objects.create(name="Added after the dump")
        Note.objects.create(text="local note")

        call_command(
            "snapshot_load",
            "--name",
            "orders.jsonl.gz",
            "--no-input",
            stdout=io.StringIO(),
        )

        self.assertEqual(list(Customer.objects.values_list("name", flat=True)), ["Ada"])
        order = Order.objects.get()
        self.assertEqual(order.total, Decimal("12.50"))
        self.assertEqual([tag.name for tag in order.tags.all()], ["priority"])
        # Not in the snapshot, so not touched.
        self.assertEqual(Note.objects.count(), 2)

    def test_a_broken_snapshot_changes_nothing(self):
        seed()
        path = self.downloads / "broken.jsonl.gz"
        with gzip.open(path, "wt") as handle:
            handle.write(
                '{"model": "testapp.customer", "pk": 1, "fields": {"name": "X"}}\n'
            )
            handle.write(
                '{"model": "testapp.order", "pk": 1, "fields": {"customer": 999, "total": "1"}}\n'
            )

        with snapshots(LOAD_DIRS=[str(self.downloads)]), self.assertRaises(Exception):
            operations.load(source_path=str(path), log=lambda message: None)

        self.assertEqual(Customer.objects.get().name, "Ada")
        self.assertEqual(Order.objects.count(), 1)

    def test_a_downloaded_file_loads_from_load_dirs_only(self):
        seed()
        call_command(
            "snapshot_dump",
            "testapp.Customer",
            "--name",
            "c.jsonl.gz",
            stdout=io.StringIO(),
        )
        downloaded = self.downloads / "c.jsonl.gz"
        shutil.copy(operations.storage().path("db_snapshots/c.jsonl.gz"), downloaded)
        Customer.objects.update(name="changed")

        with snapshots(LOAD_DIRS=[str(self.downloads)]):
            operations.load(source_path=str(downloaded), log=lambda message: None)
            with self.assertRaises(ValueError):
                operations.load(source_path="/etc/passwd", log=lambda message: None)
        self.assertEqual(Customer.objects.get().name, "Ada")

    def test_loading_is_refused_where_it_is_off(self):
        with snapshots(ALLOW_LOAD=False), self.assertRaisesMessage(
            CommandError, "not allowed"
        ):
            call_command("snapshot_load", "--name", "backup.jsonl.gz", "--no-input")

    def test_after_load_hooks_run_inside_the_load(self):
        seed()
        call_command(
            "snapshot_dump",
            "testapp.Customer",
            "--name",
            "c.jsonl.gz",
            stdout=io.StringIO(),
        )
        with snapshots(AFTER_LOAD=["tests.hooks.rename_customers"]):
            operations.load("c.jsonl.gz", log=lambda message: None)
        self.assertEqual(Customer.objects.get().name, "scrubbed")


@snapshots()
class RunnerTests(TestCase):
    def test_worker_mode_queues_until_the_worker_runs(self):
        seed()
        snapshot = Snapshot.objects.create(kind=Snapshot.Kind.DUMP, name="w.jsonl.gz")
        runner.submit(snapshot)
        self.assertEqual(Snapshot.objects.get().status, Snapshot.Status.PENDING)

        call_command("snapshot_worker", stdout=io.StringIO())

        snapshot.refresh_from_db()
        self.assertEqual(snapshot.status, Snapshot.Status.SUCCEEDED, snapshot.log)
        self.assertGreater(snapshot.size, 0)
        self.assertGreater(snapshot.objects_count, 0)

    def test_a_failure_is_recorded_with_its_reason(self):
        snapshot = Snapshot.objects.create(
            kind=Snapshot.Kind.LOAD, name="missing.jsonl.gz"
        )
        runner.execute(snapshot.pk)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.status, Snapshot.Status.FAILED)
        self.assertTrue(snapshot.error)

    def test_tasks_mode_uses_the_task_framework_when_there_is_one(self):
        if runner.task is None:
            self.skipTest("No task framework installed.")
        seed()
        snapshot = Snapshot.objects.create(kind=Snapshot.Kind.DUMP, name="t.jsonl.gz")
        with snapshots(RUNNER="tasks"):
            self.assertTrue(runner.runs_inline())  # the default ImmediateBackend
            runner.submit(snapshot)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.status, Snapshot.Status.SUCCEEDED, snapshot.log)


@snapshots()
class AdminTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            "admin", "a@example.com", "pw"
        )
        self.client.force_login(self.user)

    def test_the_dump_page_lists_models_and_queues_a_dump(self):
        seed()
        page = self.client.get(reverse("admin:db_snapshots_snapshot_dump"))
        self.assertContains(page, 'value="testapp.Order"')
        self.assertNotContains(page, 'value="sessions.Session"')

        response = self.client.post(
            reverse("admin:db_snapshots_snapshot_dump"),
            {
                "name": "orders.jsonl.gz",
                "models": ["testapp.Order"],
                "include_dependencies": "on",
            },
        )

        snapshot = Snapshot.objects.get()
        self.assertRedirects(
            response,
            reverse("admin:db_snapshots_snapshot_change", args=[snapshot.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(snapshot.requested_by, "admin")
        self.assertIn("testapp.Customer", snapshot.model_labels)

    def test_a_finished_dump_downloads_with_its_size(self):
        seed()
        snapshot = Snapshot.objects.create(kind=Snapshot.Kind.DUMP, name="d.jsonl.gz")
        runner.execute(snapshot.pk)

        listing = self.client.get(reverse("admin:db_snapshots_snapshot_changelist"))
        response = self.client.get(
            reverse("admin:db_snapshots_snapshot_download", args=["d.jsonl.gz"])
        )

        self.assertContains(listing, "download")
        body = b"".join(response.streaming_content)
        self.assertEqual(int(response["Content-Length"]), len(body))
        self.assertTrue(gzip.decompress(body).startswith(b'{"model": '))

    def test_the_load_page_needs_the_confirmation(self):
        seed()
        runner.execute(
            Snapshot.objects.create(kind=Snapshot.Kind.DUMP, name="l.jsonl.gz").pk
        )
        url = reverse("admin:db_snapshots_snapshot_load")

        self.assertContains(self.client.get(url), "l.jsonl.gz")
        self.client.post(url, {"source": "storage:l.jsonl.gz"})
        self.assertFalse(Snapshot.objects.filter(kind=Snapshot.Kind.LOAD).exists())
        self.client.post(url, {"source": "storage:l.jsonl.gz", "confirm": "on"})
        self.assertTrue(Snapshot.objects.filter(kind=Snapshot.Kind.LOAD).exists())

    def test_where_loading_is_off_the_page_and_button_are_gone(self):
        with snapshots(ALLOW_LOAD=False):
            listing = self.client.get(reverse("admin:db_snapshots_snapshot_changelist"))
            page = self.client.get(reverse("admin:db_snapshots_snapshot_load"))
        self.assertNotContains(listing, "Load a snapshot")
        self.assertEqual(page.status_code, 404)


class CheckTests(TestCase):
    def test_loading_with_debug_off_is_warned_about(self):
        with snapshots(), override_settings(DEBUG=False):
            warnings = checks.load_allowed_in_production()
        self.assertEqual([warning.id for warning in warnings], ["db_snapshots.W001"])
        self.assertIsInstance(warnings[0], CheckWarning)
        with snapshots(ALLOW_LOAD=False), override_settings(DEBUG=False):
            self.assertEqual(checks.load_allowed_in_production(), [])
