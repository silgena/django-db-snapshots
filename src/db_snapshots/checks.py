from django.conf import settings
from django.core import checks

from db_snapshots import conf


@checks.register(checks.Tags.database)
def load_allowed_in_production(app_configs=None, **kwargs):
    """A load replaces data; it should not be switched on where DEBUG is off."""
    if conf.get("ALLOW_LOAD") and not settings.DEBUG:
        return [
            checks.Warning(
                "DB_SNAPSHOTS['ALLOW_LOAD'] is on with DEBUG off.",
                hint=(
                    "A load replaces this database's data. Allow it only on "
                    "development machines; servers should only dump."
                ),
                id="db_snapshots.W001",
            )
        ]
    return []
