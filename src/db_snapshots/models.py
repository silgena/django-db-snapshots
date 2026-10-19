from django.db import models


class Snapshot(models.Model):
    """One dump or load: what it covered, who asked, how it ended.

    The person is stored by name rather than by foreign key on purpose. A
    load may replace the user table; a foreign key from here would stop it
    being emptied, and the history should outlive the accounts anyway.
    """

    class Kind(models.TextChoices):
        DUMP = "dump", "Dump"
        LOAD = "load", "Load"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=4, choices=Kind.choices)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    #: The file in the snapshots storage, e.g. "backup.jsonl.gz". A dump to a
    #: name that exists overwrites it.
    name = models.CharField(max_length=255)
    #: For a load from a local folder rather than the storage.
    source_path = models.CharField(max_length=1024, blank=True, default="")
    #: Model labels ("app_label.ModelName") dumped or replaced; empty is all.
    model_labels = models.JSONField(default=list, blank=True)
    requested_by = models.CharField(max_length=150, blank=True, default="")
    note = models.CharField(max_length=500, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    size = models.PositiveBigIntegerField(null=True, blank=True)
    objects_count = models.PositiveBigIntegerField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    log = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.name} ({self.get_status_display()})"

    @property
    def is_active(self):
        return self.status in (self.Status.PENDING, self.Status.RUNNING)

    @property
    def duration(self):
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return None
