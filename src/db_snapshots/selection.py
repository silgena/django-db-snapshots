"""Which models a snapshot can hold, and what a selection depends on.

A dump of orders without the customers they point at cannot be loaded: every
order fails its foreign key. So a selection is closed over its dependencies -
choosing ``shop.Order`` brings ``shop.Customer`` along - except for models
that every database creates for itself (content types, permissions), which
are written as natural keys and resolved against the target's own rows.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.apps import apps

from db_snapshots import conf


def _matches(model, labels) -> bool:
    meta = model._meta
    lowered = {label.lower() for label in labels}
    return meta.app_label.lower() in lowered or meta.label_lower in lowered


def is_excluded(model) -> bool:
    return _matches(model, conf.get("EXCLUDE"))


def snapshot_models() -> list:
    """Every model a snapshot may contain: concrete, managed, not excluded."""
    return [
        model
        for model in apps.get_models()
        if not model._meta.proxy and model._meta.managed and not is_excluded(model)
    ]


def resolve(labels) -> list:
    """Models for "app_label" or "app_label.ModelName" labels, in app order."""
    wanted = snapshot_models()
    if not labels:
        return wanted
    chosen = [model for model in wanted if _matches(model, labels)]
    unknown = [
        label
        for label in labels
        if not any(_matches(model, [label]) for model in wanted)
    ]
    if unknown:
        raise LookupError("Not a model a snapshot can contain: " + ", ".join(unknown))
    return chosen


def dependencies(model) -> set:
    """Models this one points at through a foreign key, one-to-one or M2M."""
    targets = set()
    for field in model._meta.get_fields():
        if field.is_relation and field.concrete and field.related_model:
            targets.add(field.related_model._meta.concrete_model)
        if field.many_to_many and not field.auto_created and field.related_model:
            targets.add(field.related_model._meta.concrete_model)
    targets.discard(model)
    return targets


def with_dependencies(models) -> list:
    """The selection plus everything it needs to load, in app order."""
    allowed = set(snapshot_models())
    closed = set(models)
    pending = list(models)
    while pending:
        for target in dependencies(pending.pop()):
            if target in allowed and target not in closed:
                closed.add(target)
                pending.append(target)
    return [model for model in snapshot_models() if model in closed]


@dataclass(frozen=True)
class AppGroup:
    label: str
    name: str
    models: list


def grouped() -> list[AppGroup]:
    """Snapshot models grouped by app, for the selection page."""
    groups: dict[str, AppGroup] = {}
    for model in snapshot_models():
        config = model._meta.app_config
        group = groups.setdefault(
            config.label, AppGroup(config.label, str(config.verbose_name), [])
        )
        group.models.append(model)
    return list(groups.values())


def labels(models) -> list[str]:
    return [model._meta.label for model in models]
