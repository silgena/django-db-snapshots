from django.core.management import BaseCommand, CommandError

from db_snapshots import conf, operations, selection


class Command(BaseCommand):
    help = (
        "Dump models to a snapshot in the snapshots storage, read in one "
        "consistent transaction. Overwrites a snapshot of the same name."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "args",
            metavar="app_label[.ModelName]",
            nargs="*",
            help="Models to dump; every model if none.",
        )
        parser.add_argument(
            "--name", default=None, help="File name (default: BACKUP_NAME)."
        )
        parser.add_argument(
            "--no-dependencies",
            action="store_true",
            help="Do not add the models the selection points at.",
        )

    def handle(self, *labels, name, no_dependencies, **options):
        if not conf.get("ALLOW_DUMP"):
            raise CommandError(
                "Dumping is not allowed in this environment (DB_SNAPSHOTS['ALLOW_DUMP'])."
            )
        try:
            models = selection.resolve(labels)
            if labels and not no_dependencies:
                models = selection.with_dependencies(models)
            operations.dump(
                name or conf.get("BACKUP_NAME"),
                selection.labels(models),
                log=self.stdout.write,
            )
        except (LookupError, ValueError) as exc:
            raise CommandError(str(exc))
