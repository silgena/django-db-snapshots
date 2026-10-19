from django.core.management import BaseCommand, CommandError

from db_snapshots import conf, operations


class Command(BaseCommand):
    help = (
        "Dump every model to BACKUP_NAME, overwriting the previous backup. "
        "Schedule it nightly (cron, a task scheduler) to keep exactly one file."
    )

    def handle(self, *args, **options):
        if not conf.get("ALLOW_DUMP"):
            raise CommandError(
                "Dumping is not allowed in this environment (DB_SNAPSHOTS['ALLOW_DUMP'])."
            )
        operations.dump(conf.get("BACKUP_NAME"), [], log=self.stdout.write)
