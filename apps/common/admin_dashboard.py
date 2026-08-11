"""
Bill Munshi admin dashboard callback.

Wired via `UNFOLD["DASHBOARD_CALLBACK"]` in `config/settings/base.py`.
Returns a context dict that unfold renders into the admin root page.
Layout: 4 KPI cards + 2 tables (recent bills + open support tickets).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)


def _safe_count(model_path):
    """Import a model by 'app_label.Model' string and return .objects.count()
    without ever crashing the dashboard on a missing app/migration."""
    try:
        app_label, model_name = model_path.split(".")
        from django.apps import apps
        model = apps.get_model(app_label, model_name)
        return model.objects.count()
    except Exception as e:
        logger.debug("dashboard count %s failed: %s", model_path, e)
        return 0


def _safe_qs(model_path, order_by="-created_at", limit=10, filters=None):
    """Return a queryset slice or [] on any exception."""
    try:
        app_label, model_name = model_path.split(".")
        from django.apps import apps
        model = apps.get_model(app_label, model_name)
        qs = model.objects.all()
        if filters:
            qs = qs.filter(**filters)
        return list(qs.order_by(order_by)[:limit])
    except Exception as e:
        logger.debug("dashboard qs %s failed: %s", model_path, e)
        return []


def dashboard_callback(request, context):
    """
    Called by django-unfold to build the /admin/ landing dashboard.

    Adds:
      - `kpi`: list of headline stat cards
      - `recent_bills`: last 10 bills across all three voucher types
      - `open_tickets`: last 10 open support tickets
    """
    User = get_user_model()

    org_count = _safe_count("organizations.Organization")
    user_count = User.objects.count() if User else 0
    vendor_bill_count = _safe_count("tally.TallyVendorBill")
    expense_bill_count = _safe_count("tally.TallyExpenseBill")
    payment_bill_count = _safe_count("tally.TallyPaymentBill")
    total_bills = vendor_bill_count + expense_bill_count + payment_bill_count
    open_ticket_count = 0
    try:
        from apps.support.models import SupportTicket
        open_ticket_count = SupportTicket.objects.filter(
            status__in=("open", "in_progress")
        ).count()
    except Exception:
        pass

    # Bills synced-to-Tally rate over the last 7 days.
    synced_rate = None
    try:
        cutoff = timezone.now() - timedelta(days=7)
        from apps.module.tally.models import (
            TallyVendorBill,
            TallyExpenseBill,
            TallyPaymentBill,
        )
        recent = 0
        synced = 0
        for _m in (TallyVendorBill, TallyExpenseBill, TallyPaymentBill):
            recent += _m.objects.filter(created_at__gte=cutoff).count()
            synced += _m.objects.filter(
                created_at__gte=cutoff, tally_synced=True,
            ).count()
        synced_rate = (synced * 100.0 / recent) if recent else None
    except Exception:
        pass

    kpi = [
        {
            "title": "Organizations",
            "value": f"{org_count:,}",
            "footer": f"{user_count:,} users",
            "url": reverse("admin:organizations_organization_changelist"),
            "icon": "domain",
        },
        {
            "title": "Bills processed",
            "value": f"{total_bills:,}",
            "footer": (
                f"{vendor_bill_count:,} purchase · "
                f"{expense_bill_count:,} journal · "
                f"{payment_bill_count:,} payment"
            ),
            "icon": "receipt",
        },
        {
            "title": "Synced to Tally (7d)",
            "value": (f"{synced_rate:.1f}%" if synced_rate is not None else "—"),
            "footer": "Bills confirmed by Tally callback in the last 7 days",
            "icon": "cloud_done",
        },
        {
            "title": "Open support tickets",
            "value": f"{open_ticket_count:,}",
            "footer": "Open + in-progress across all organisations",
            "url": _try_reverse("admin:support_supportticket_changelist"),
            "icon": "help",
        },
    ]

    recent_bills = []
    for path, kind in (
        ("tally.TallyVendorBill", "Purchase"),
        ("tally.TallyExpenseBill", "Journal"),
        ("tally.TallyPaymentBill", "Payment"),
    ):
        for b in _safe_qs(path, limit=5):
            recent_bills.append({
                "id": str(getattr(b, "id", "")),
                "name": getattr(b, "bill_munshi_name", "") or str(b),
                "status": getattr(b, "status", ""),
                "tally_synced": getattr(b, "tally_synced", False),
                "kind": kind,
                "created_at": getattr(b, "created_at", None),
                "org": (
                    getattr(getattr(b, "organization", None), "name", "")
                    or ""
                ),
            })
    recent_bills.sort(key=lambda r: r["created_at"] or timezone.now(), reverse=True)
    recent_bills = recent_bills[:10]

    open_tickets = []
    try:
        from apps.support.models import SupportTicket
        for t in SupportTicket.objects.filter(
            status__in=("open", "in_progress"),
        ).order_by("-created_at")[:10]:
            open_tickets.append({
                "id": str(t.id),
                "subject": t.subject,
                "status": t.status,
                "category": getattr(t, "category", ""),
                "created_at": t.created_at,
                "org": getattr(getattr(t, "organization", None), "name", "") or "",
            })
    except Exception:
        pass

    context.update({
        "kpi": kpi,
        "recent_bills": recent_bills,
        "open_tickets": open_tickets,
    })
    return context


def _try_reverse(name):
    try:
        return reverse(name)
    except Exception:
        return None
