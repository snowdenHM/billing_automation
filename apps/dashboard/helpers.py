"""
Generic dashboard helpers for building overview / funnel / usage responses.

Both Tally and Zoho dashboard views share the same structure — only the
models and timestamp field names differ.  This module provides thin
factory helpers so each view file becomes a handful of one-liners.

Payment vouchers exist only on the Tally side, so every builder takes an
optional ``payment_bill_model``. When it is omitted (Zoho) the response
keeps its original shape; when it is given the payment keys are added
alongside the vendor / expense ones.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from apps.organizations.models import Organization


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _visible(bill_model):
    """Base queryset for a bill model, with trashed rows excluded.

    These helpers are shared by the Tally and Zoho dashboards. Only the
    Tally bill models carry ``TrashableMixin``, so the trash filter is
    applied on capability rather than unconditionally — Zoho models have
    no ``is_deleted`` column and pass straight through.
    """
    manager = bill_model.objects
    return manager.alive() if hasattr(manager, "alive") else manager.all()


def _get_organization(org_id):
    """Return ``(organization, None)`` or ``(None, error_response)``."""
    try:
        return Organization.objects.get(id=org_id), None
    except Organization.DoesNotExist:
        return None, Response({"error": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)


def _total_amount(analyzed_model, organization, amount_field, bill_field=None):
    """Sum ``amount_field`` across an analysed-bill model for one org.

    Summed in Python rather than with ``Sum()``: the Zoho analysed models
    keep ``total`` in a CharField, which PostgreSQL cannot SUM.

    ``bill_field`` names the FK from an analysed row to its uploaded bill.
    When given, rows whose bill sits in the trash are skipped, so the amount
    agrees with the counts beside it (those come from ``_visible``).
    """
    qs = analyzed_model.objects.filter(organization=organization)
    if bill_field:
        qs = qs.filter(**{f"{bill_field}__is_deleted": False})
    return sum(float(value or 0) for value in qs.values_list(amount_field, flat=True))


def _calculate_conversion_rates(funnel_data):
    """Return analysis / verification / sync rates for a funnel dict.

    Each rate is that stage's own share of the uploaded total, matching the
    stage counts shown beside it on the dashboard.

    These previously accumulated downstream stages — ``analysis_rate`` was
    ``(analysed + verified + synced) / total`` — on the reasoning that a
    synced bill must have been analysed at some point. But ``status`` holds a
    single value, so the counts are mutually exclusive buckets: a synced bill
    is counted only in ``synced``. The card therefore rendered "Analysed 0"
    directly above "Analysis 100.0%", and 3-of-8 analysed read as 100%
    instead of 37.5%.

    Note this makes the bars a snapshot of where bills currently sit, not a
    monotonically decreasing funnel. Showing true funnel throughput would
    mean tracking stage timestamps (or cumulative counts) rather than
    deriving history from the current status.
    """
    total = funnel_data["total_uploaded"]
    if total == 0:
        return {"analysis_rate": 0.0, "verification_rate": 0.0, "sync_rate": 0.0}
    return {
        "analysis_rate": funnel_data["analysed"] / total * 100,
        "verification_rate": funnel_data["verified"] / total * 100,
        "sync_rate": funnel_data["synced"] / total * 100,
    }


def _bill_stats(qs):
    """Return standard stats dict for a bill queryset."""
    return {
        "total_count": qs.count(),
        "draft_count": qs.filter(status="Draft").count(),
        "analysed_count": qs.filter(status__in=["Analysed", "Verified"]).count(),
        "synced_count": qs.filter(status="Synced").count(),
    }


def _funnel(qs):
    """Return standard funnel dict for a bill queryset."""
    data = {
        "total_uploaded": qs.count(),
        "draft": qs.filter(status="Draft").count(),
        "analysed": qs.filter(status="Analysed").count(),
        "verified": qs.filter(status="Verified").count(),
        "synced": qs.filter(status="Synced").count(),
    }
    data["conversion_rates"] = _calculate_conversion_rates(data)
    return data


# ---------------------------------------------------------------------------
# Generic view builders
# ---------------------------------------------------------------------------

def build_overview_response(
    org_id,
    *,
    vendor_bill_model,
    expense_bill_model,
    analyzed_vendor_model,
    analyzed_expense_model,
    counter_model,
    payment_bill_model=None,
    analyzed_payment_model=None,
    analyzed_bill_field=None,
    amount_field="total",
    counter_label="vendor_count",
):
    """Build the overview response dict used by both Tally and Zoho dashboards.

    ``analyzed_bill_field`` — FK from the analysed models to their uploaded
    bill; pass it when the bills are trashable so trashed bills drop out of
    the amounts as they already do from the counts.
    """
    organization, err = _get_organization(org_id)
    if err:
        return err

    vendor_qs = _visible(vendor_bill_model).filter(organization=organization)
    expense_qs = _visible(expense_bill_model).filter(organization=organization)

    total_vendor = _total_amount(
        analyzed_vendor_model, organization, amount_field, analyzed_bill_field,
    )
    total_expense = _total_amount(
        analyzed_expense_model, organization, amount_field, analyzed_bill_field,
    )

    week_ago = timezone.now() - timedelta(days=7)

    payload = {
        "vendor_bills": _bill_stats(vendor_qs),
        "expense_bills": _bill_stats(expense_qs),
        "financial_summary": {
            "total_vendor_amount": total_vendor,
            "total_expense_amount": total_expense,
            "combined_amount": total_vendor + total_expense,
        },
        counter_label: counter_model.objects.filter(organization=organization).count(),
        "recent_activity": {
            "vendor_bills_last_7_days": vendor_qs.filter(created_at__gte=week_ago).count(),
            "expense_bills_last_7_days": expense_qs.filter(created_at__gte=week_ago).count(),
        },
    }

    if payment_bill_model is not None:
        payment_qs = _visible(payment_bill_model).filter(organization=organization)
        total_payment = _total_amount(
            analyzed_payment_model, organization, amount_field, analyzed_bill_field,
        )

        payload["payment_bills"] = _bill_stats(payment_qs)
        payload["financial_summary"]["total_payment_amount"] = total_payment
        payload["financial_summary"]["combined_amount"] += total_payment
        payload["recent_activity"]["payment_bills_last_7_days"] = (
            payment_qs.filter(created_at__gte=week_ago).count()
        )

    return Response(payload)


def build_funnel_response(
    org_id,
    *,
    vendor_bill_model,
    expense_bill_model,
    payment_bill_model=None,
):
    """Build the funnel response dict used by both Tally and Zoho dashboards."""
    organization, err = _get_organization(org_id)
    if err:
        return err

    payload = {
        "vendor_bills_funnel": _funnel(_visible(vendor_bill_model).filter(organization=organization)),
        "expense_bills_funnel": _funnel(_visible(expense_bill_model).filter(organization=organization)),
    }
    if payment_bill_model is not None:
        payload["payment_bills_funnel"] = _funnel(
            _visible(payment_bill_model).filter(organization=organization)
        )
    return Response(payload)


def build_usage_response(
    org_id,
    *,
    vendor_bill_model,
    expense_bill_model,
    payment_bill_model=None,
    updated_at_field="updated_at",
):
    """Build the usage response dict used by both Tally and Zoho dashboards."""
    organization, err = _get_organization(org_id)
    if err:
        return err

    # Keyed by the prefix each bill type contributes to the response
    # (``vendor_bills_uploaded``, ``total_vendor_files`` …).
    bill_models = {"vendor": vendor_bill_model, "expense": expense_bill_model}
    if payment_bill_model is not None:
        bill_models["payment"] = payment_bill_model
    querysets = {
        kind: _visible(model).filter(organization=organization)
        for kind, model in bill_models.items()
    }

    now = timezone.now()
    usage_stats = {}
    for period_name, days in [("today", 1), ("week", 7), ("month", 30), ("quarter", 90)]:
        start_date = now - timedelta(days=days)
        kw_analysed = {f"{updated_at_field}__gte": start_date}

        period = {
            f"{kind}_bills_uploaded": qs.filter(created_at__gte=start_date).count()
            for kind, qs in querysets.items()
        }
        period["bills_analysed"] = sum(
            qs.filter(status__in=["Analysed", "Verified", "Synced"], **kw_analysed).count()
            for qs in querysets.values()
        )
        period["bills_synced"] = sum(
            qs.filter(status="Synced", **kw_analysed).count()
            for qs in querysets.values()
        )
        usage_stats[period_name] = period

    file_counts = {
        kind: qs.filter(file__isnull=False).count()
        for kind, qs in querysets.items()
    }
    file_statistics = {f"total_{kind}_files": count for kind, count in file_counts.items()}
    file_statistics["total_files"] = sum(file_counts.values())

    return Response({
        "usage_by_period": usage_stats,
        "file_statistics": file_statistics,
    })
