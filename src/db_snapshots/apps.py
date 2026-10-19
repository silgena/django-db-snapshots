from django.apps import AppConfig


class DbSnapshotsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "db_snapshots"
    verbose_name = "Database snapshots"

    def ready(self):
        from db_snapshots import checks  # noqa: F401 - registers the checks
