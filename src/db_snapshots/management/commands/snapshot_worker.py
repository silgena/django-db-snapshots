from django.core.management import BaseCommand

from db_snapshots import runner


class Command(BaseCommand):
    help = (
        "Run snapshots queued from the admin when no task framework is in use "
        "(DB_SNAPSHOTS['RUNNER'] = 'worker'). Run it from cron every minute."
    )

    def handle(self, *args, **options):
        done = runner.run_pending()
        self.stdout.write(f"Ran {done} snapshot(s).")
