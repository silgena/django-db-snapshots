from django.core.management import BaseCommand, CommandError

from db_snapshots import operations


class Command(BaseCommand):
    help = (
        "Replace the data of the models in a snapshot with the snapshot's, in "
        "one transaction. Refused unless DB_SNAPSHOTS['ALLOW_LOAD'] is on."
    )

    def add_arguments(self, parser):
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--name", help="A snapshot in the snapshots storage.")
        source.add_argument("--path", help="A snapshot file in one of LOAD_DIRS.")
        parser.add_argument("--no-input", action="store_false", dest="interactive")

    def handle(self, *args, name, path, interactive, **options):
        if interactive:
            answer = input(
                "This replaces the data of every model in the snapshot. Continue? [y/N] "
            )
            if answer.strip().lower() != "y":
                raise CommandError("Load cancelled.")
        try:
            operations.load(name or "", source_path=path or "", log=self.stdout.write)
        except ValueError as exc:
            raise CommandError(str(exc))
