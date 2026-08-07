"""
Generic dashboard helpers for building overview / funnel / usage responses.

Both Tally and Zoho dashboard views share the same structure — only the
models and timestamp field names differ.  This module provides thin
factory helpers so each view file becomes a handful of one-liners.
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
    amount_field="total",
    counter_label="vendor_count",
):
    """Build the overview response dict used by both Tally and Zoho dashboards."""
    organization, err = _get_organization(org_id)
    if err:
        return err

    vendor_qs = _visible(vendor_bill_model).filter(organization=organization)
    expense_qs = _visible(expense_bill_model).filter(organization=organization)

    analyzed_vendor_qs = analyzed_vendor_model.objects.filter(organization=organization)
    analyzed_expense_qs = analyzed_expense_model.objects.filter(organization=organization)

    total_vendor = sum(
        float(getattr(b, amount_field) or 0) for b in analyzed_vendor_qs
    )
    total_expense = sum(
        float(getattr(b, amount_field) or 0) for b in analyzed_expense_qs
    )

    week_ago = timezone.now() - timedelta(days=7)

    return Response({
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
    })


def build_funnel_response(org_id, *, vendor_bill_model, expense_bill_model):
    """Build the funnel response dict used by both Tally and Zoho dashboards."""
    organization, err = _get_organization(org_id)
    if err:
        return err

    return Response({
        "vendor_bills_funnel": _funnel(_visible(vendor_bill_model).filter(organization=organization)),
        "expense_bills_funnel": _funnel(_visible(expense_bill_model).filter(organization=organization)),
    })


def build_usage_response(
    org_id,
    *,
    vendor_bill_model,
    expense_bill_model,
    updated_at_field="updated_at",
):
    """Build the usage response dict used by both Tally and Zoho dashboards."""
    organization, err = _get_organization(org_id)
    if err:
        return err

    now = timezone.now()
    usage_stats = {}
    for period_name, days in [("today", 1), ("week", 7), ("month", 30), ("quarter", 90)]:
        start_date = now - timedelta(days=days)
        vendor_qs = _visible(vendor_bill_model).filter(organization=organization)
        expense_qs = _visible(expense_bill_model).filter(organization=organization)

        kw_analysed = {f"{updated_at_field}__gte": start_date}
        usage_stats[period_name] = {
            "vendor_bills_uploaded": vendor_qs.filter(created_at__gte=start_date).count(),
            "expense_bills_uploaded": expense_qs.filter(created_at__gte=start_date).count(),
            "bills_analysed": (
                vendor_qs.filter(status__in=["Analysed", "Verified", "Synced"], **kw_analysed).count()
                + expense_qs.filter(status__in=["Analysed", "Verified", "Synced"], **kw_analysed).count()
            ),
            "bills_synced": (
                vendor_qs.filter(status="Synced", **kw_analysed).count()
                + expense_qs.filter(status="Synced", **kw_analysed).count()
            ),
        }

    vendor_files = _visible(vendor_bill_model).filter(organization=organization, file__isnull=False).count()
    expense_files = _visible(expense_bill_model).filter(organization=organization, file__isnull=False).count()

    return Response({
        "usage_by_period": usage_stats,
        "file_statistics": {
            "total_vendor_files": vendor_files,
            "total_expense_files": expense_files,
            "total_files": vendor_files + expense_files,
        },
    })
