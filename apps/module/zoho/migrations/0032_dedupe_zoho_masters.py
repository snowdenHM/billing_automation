"""Collapse duplicate Zoho master rows before a unique key is put on them.

The masters never had a key on their Zoho id, so two syncs running at
once could each insert the same vendor. This runs before the unique
constraints are added, because adding one to a table that already holds
duplicates fails and takes the whole deploy with it.

Duplicates are **repointed, not deleted outright**. Thirty foreign keys
reach these tables from analysed bill data — every line's account and
tax, every bill's vendor. Dropping a duplicate without moving those
references first would blank the mapping on any bill that happened to
reference the losing row, and that bill would then need mapping again
by hand with nothing on screen to say why.

The oldest row wins, since it is the one bills have had the longest to
reference.
"""

from django.db import migrations

#: (model, the fields that should have been unique all along)
TARGETS = [
    ("ZohoVendor", ["organization_id", "contactId"]),
    ("ZohoCustomer", ["organization_id", "contactId"]),
    ("ZohoChartOfAccount", ["organization_id", "accountId"]),
    ("ZohoTaxes", ["organization_id", "taxId"]),
    ("ZohoTdsTcs", ["organization_id", "taxId", "taxType"]),
]


def _dedupe(apps, schema_editor):
    from collections import defaultdict

    for model_name, key_fields in TARGETS:
        model = apps.get_model("zoho", model_name)

        groups = defaultdict(list)
        for row in model.objects.all().order_by("created_at", "id"):
            groups[tuple(getattr(row, f) for f in key_fields)].append(row)

        # Every FK aimed at this model, so references can be moved
        # rather than lost.
        inbound = [
            (rel.related_model, rel.field.name)
            for rel in model._meta.related_objects
        ]

        for rows in groups.values():
            if len(rows) < 2:
                continue
            keeper, losers = rows[0], rows[1:]
            loser_ids = [r.pk for r in losers]

            for child_model, field_name in inbound:
                child_model.objects.filter(
                    **{f"{field_name}__in": loser_ids}
                ).update(**{field_name: keeper.pk})

            model.objects.filter(pk__in=loser_ids).delete()


def _dedupe_credentials(apps, schema_editor):
    """One credentials row per workspace.

    Handled apart from the masters because "oldest wins" is the wrong
    rule here: of two rows the one that is actually connected is the
    one worth keeping, whenever it was made. Keeping a stale row would
    disconnect a working integration.
    """
    from collections import defaultdict

    model = apps.get_model("zoho", "ZohoCredentials")

    groups = defaultdict(list)
    for row in model.objects.all():
        groups[row.organization_id].append(row)

    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows.sort(
            key=lambda r: (
                bool(r.is_connected),
                bool(r.refreshToken),
                bool(r.organisationId),
                r.update_at,
            ),
            reverse=True,
        )
        model.objects.filter(
            pk__in=[r.pk for r in rows[1:]]
        ).delete()


def _noop(apps, schema_editor):
    """Irreversible: the collapsed rows cannot be told apart again."""


class Migration(migrations.Migration):

    dependencies = [
        ("zoho", "0031_zoho_region"),
    ]

    operations = [
        migrations.RunPython(_dedupe, _noop),
        migrations.RunPython(_dedupe_credentials, _noop),
    ]
