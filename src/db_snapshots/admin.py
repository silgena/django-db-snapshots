from django import forms
from django.contrib import admin, messages
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from db_snapshots import conf, operations, runner, selection
from db_snapshots.models import Snapshot

STATUS_COLOURS = {
    Snapshot.Status.PENDING: "#57606a",
    Snapshot.Status.RUNNING: "#0969da",
    Snapshot.Status.SUCCEEDED: "#1a7f37",
    Snapshot.Status.FAILED: "#cf222e",
}


class _LargeFileResponse(FileResponse):
    # Megabyte reads: a snapshot is often hundreds of megabytes.
    block_size = 1024 * 1024


def _human(size):
    if size is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


class DumpForm(forms.Form):
    name = forms.CharField(
        max_length=255,
        help_text=f"A snapshot of this name is overwritten. Ends in {operations.SUFFIX}.",
    )
    models = forms.MultipleChoiceField(
        required=False, widget=forms.CheckboxSelectMultiple
    )
    include_dependencies = forms.BooleanField(
        required=False,
        initial=True,
        help_text="Also dump the models the selection points at, so it can be loaded.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["models"].choices = [
            (model._meta.label, model._meta.label)
            for model in selection.snapshot_models()
        ]

    def clean_name(self):
        try:
            return operations.check_name(self.cleaned_data["name"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc))


@admin.register(Snapshot)
class SnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "kind",
        "name",
        "coloured_status",
        "models_count",
        "file_size",
        "requested_by",
        "took",
        "download",
    )
    list_filter = ("kind", "status")
    search_fields = ("name", "requested_by", "note")
    change_list_template = "admin/db_snapshots/snapshot/change_list.html"
    change_form_template = "admin/db_snapshots/snapshot/change_form.html"
    readonly_fields = (
        "kind",
        "name",
        "coloured_status",
        "requested_by",
        "note",
        "source_path",
        "created_at",
        "started_at",
        "finished_at",
        "took",
        "file_size",
        "objects_count",
        "model_list",
        "error",
    )
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def _can_run(self, request):
        return request.user.has_perm("db_snapshots.add_snapshot")

    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path("dump/", view(self.dump_view), name="db_snapshots_snapshot_dump"),
            path("load/", view(self.load_view), name="db_snapshots_snapshot_load"),
            path(
                "file/<str:name>/",
                view(self.download_view),
                name="db_snapshots_snapshot_download",
            ),
        ] + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        extra_context = {
            **(extra_context or {}),
            "allow_dump": conf.get("ALLOW_DUMP") and self._can_run(request),
            "allow_load": conf.get("ALLOW_LOAD") and self._can_run(request),
        }
        return super().changelist_view(request, extra_context)

    # -- running --------------------------------------------------------
    def _start(self, request, **fields):
        snapshot = Snapshot.objects.create(
            requested_by=request.user.get_username(), **fields
        )
        try:
            runner.submit(snapshot)
        except Exception as exc:
            Snapshot.objects.filter(pk=snapshot.pk).update(
                status=Snapshot.Status.FAILED, error=f"could not be queued: {exc}"
            )
            messages.error(request, f"Could not start it: {exc}")
        else:
            if runner.mode() == "worker":
                messages.info(
                    request, "Queued. `manage.py snapshot_worker` will run it."
                )
        return redirect(
            reverse("admin:db_snapshots_snapshot_change", args=[snapshot.pk])
        )

    def dump_view(self, request):
        if not (conf.get("ALLOW_DUMP") and self._can_run(request)):
            raise Http404("Dumping is not allowed here.")
        form = DumpForm(
            request.POST or None,
            initial={"name": conf.get("BACKUP_NAME"), "include_dependencies": True},
        )
        if request.method == "POST" and form.is_valid():
            models = selection.resolve(form.cleaned_data["models"])
            if (
                form.cleaned_data["models"]
                and form.cleaned_data["include_dependencies"]
            ):
                models = selection.with_dependencies(models)
            return self._start(
                request,
                kind=Snapshot.Kind.DUMP,
                name=form.cleaned_data["name"],
                model_labels=selection.labels(models),
            )
        chosen = set(form["models"].value() or [])
        groups = [
            {
                "label": group.label,
                "name": group.name,
                "models": [
                    {
                        "label": model._meta.label,
                        "name": str(model._meta.verbose_name_plural).capitalize(),
                        "needs": ", ".join(
                            label
                            for label in selection.labels(
                                selection.with_dependencies([model])
                            )
                            if label != model._meta.label
                        ),
                        "checked": model._meta.label in chosen,
                    }
                    for model in group.models
                ],
            }
            for group in selection.grouped()
        ]
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Dump a snapshot",
            "form": form,
            "groups": groups,
            "inline": runner.runs_inline(),
            "excluded": conf.get("EXCLUDE"),
        }
        return render(request, "admin/db_snapshots/snapshot/dump.html", context)

    def load_view(self, request):
        if not (conf.get("ALLOW_LOAD") and self._can_run(request)):
            raise Http404("Loading is not allowed here.")
        if request.method == "POST":
            if request.POST.get("confirm") != "on":
                messages.error(
                    request, "Tick the box to confirm the load replaces data."
                )
                return redirect(reverse("admin:db_snapshots_snapshot_load"))
            source = request.POST.get("source", "")
            kind, _, value = source.partition(":")
            try:
                if kind == "storage":
                    fields = {"name": operations.check_name(value)}
                elif kind == "local":
                    local = operations.resolve_local(value)
                    fields = {"name": local.name, "source_path": str(local)}
                else:
                    raise ValueError("Choose a snapshot to load.")
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect(reverse("admin:db_snapshots_snapshot_load"))
            return self._start(request, kind=Snapshot.Kind.LOAD, **fields)
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Load a snapshot",
            "stored": operations.stored_files(),
            "local": operations.local_files(),
            "inline": runner.runs_inline(),
        }
        return render(request, "admin/db_snapshots/snapshot/load.html", context)

    def download_view(self, request, name):
        if not self.has_view_permission(request):
            raise Http404
        try:
            operations.check_name(name)
        except ValueError:
            raise Http404
        store, location = operations.storage(), operations.storage_path(name)
        if not store.exists(location):
            raise Http404("No such snapshot.")
        response = _LargeFileResponse(
            store.open(location, "rb"), as_attachment=True, filename=name
        )
        response["Content-Length"] = str(store.size(location))
        # Stop an nginx in front from spooling the whole file before sending.
        response["X-Accel-Buffering"] = "no"
        return response

    def change_view(self, request, object_id, form_url="", extra_context=None):
        snapshot = get_object_or_404(Snapshot, pk=object_id)
        extra_context = {**(extra_context or {}), "snapshot": snapshot}
        return super().change_view(request, object_id, form_url, extra_context)

    # -- columns --------------------------------------------------------
    @admin.display(description="status", ordering="status")
    def coloured_status(self, obj):
        return format_html(
            '<b style="color:{}">{}</b>',
            STATUS_COLOURS[obj.status],
            obj.get_status_display(),
        )

    @admin.display(description="models")
    def models_count(self, obj):
        return len(obj.model_labels) if obj.model_labels else "all"

    @admin.display(description="models")
    def model_list(self, obj):
        return ", ".join(obj.model_labels) if obj.model_labels else "all"

    @admin.display(description="size", ordering="size")
    def file_size(self, obj):
        return _human(obj.size)

    @admin.display(description="took")
    def took(self, obj):
        return str(obj.duration).split(".")[0] if obj.duration else "—"

    @admin.display(description="file")
    def download(self, obj):
        if obj.kind != Snapshot.Kind.DUMP or obj.status != Snapshot.Status.SUCCEEDED:
            return "—"
        # A later dump of the same name overwrote this one's file.
        newest = (
            Snapshot.objects.filter(
                kind=Snapshot.Kind.DUMP, status=Snapshot.Status.SUCCEEDED, name=obj.name
            )
            .order_by("-finished_at")
            .values_list("pk", flat=True)
            .first()
        )
        if newest != obj.pk:
            return "replaced"
        url = reverse("admin:db_snapshots_snapshot_download", args=[obj.name])
        return format_html('<a href="{}">download</a>', url)
