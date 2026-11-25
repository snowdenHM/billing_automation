"""The two questions the Tally dashboard could not answer.

It counted bills and files well enough, but an accountant opening it
wants to know two other things, and neither was on the page:

  * where the GST stands — tax paid on purchases against tax charged on
    sales, and therefore what is payable or carried forward. These are
    the figures the return is built from, and they were only derivable
    by opening every voucher.

  * what is stuck — a bill with no party picked, one the extraction
    flagged, one analysed days ago and never synced. The funnel said how
    many were at each stage; it did not say which ones needed a person.

Read-only, and derived from rows that already exist. Nothing here writes
or changes a voucher.
"""
from datetime import timedelta

from django.db.models import Count, Sum
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.module.tally.models import (
    TallyVendorBill, TallyExpenseBill, TallyPaymentBill,
    TallySalesBill, TallyReceiptBill,
    TallyVendorAnalyzedBill, TallyExpenseAnalyzedBill,
    TallyPaymentAnalyzedBill, TallySalesAnalyzedBill,
    TallyReceiptAnalyzedBill,
)
from ..helpers import _get_organization, _visible

# Input tax is what we were charged; output tax is what we charged. A
# payment voucher settles a purchase and a receipt settles a sale, so
# they sit on the same side as the document they settle.
INPUT_SIDE = [
    ("purchase", TallyVendorBill, TallyVendorAnalyzedBill, "vendor"),
    ("journal", TallyExpenseBill, TallyExpenseAnalyzedBill, "vendor"),
    ("payment", TallyPaymentBill, TallyPaymentAnalyzedBill, "vendor"),
]
OUTPUT_SIDE = [
    ("sales", TallySalesBill, TallySalesAnalyzedBill, "customer"),
    ("receipt", TallyReceiptBill, TallyReceiptAnalyzedBill, "customer"),
]
ALL_SIDES = INPUT_SIDE + OUTPUT_SIDE


def _f(x):
    return float(x or 0)


def _tax_totals(analysed_model, organization):
    """CGST / SGST / IGST and the taxable total, trashed bills excluded."""
    rows = analysed_model.objects.filter(
        organization=organization, selected_bill__isnull=False,
    ).exclude(selected_bill__deleted_at__isnull=False)
    agg = rows.aggregate(
        cgst=Sum("cgst"), sgst=Sum("sgst"), igst=Sum("igst"), total=Sum("total"),
    )
    return {k: _f(v) for k, v in agg.items()}


@extend_schema(
    summary="Tally insights — GST position and the queue needing attention",
    tags=["Tally Dashboard"],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tally_insights_view(request, org_id):
    organization, err = _get_organization(org_id)
    if err:
        return err

    # ---- GST position ------------------------------------------------
    def side_total(side):
        out = {"cgst": 0.0, "sgst": 0.0, "igst": 0.0, "total": 0.0, "by_module": {}}
        for key, _bill, analysed, _party in side:
            t = _tax_totals(analysed, organization)
            out["by_module"][key] = t
            for f in ("cgst", "sgst", "igst", "total"):
                out[f] += t[f]
        out["tax"] = out["cgst"] + out["sgst"] + out["igst"]
        return out

    inp = side_total(INPUT_SIDE)
    outp = side_total(OUTPUT_SIDE)

    # Positive = payable to the department; negative = credit carried.
    net = outp["tax"] - inp["tax"]

    # ---- what needs a person ----------------------------------------
    week_ago = timezone.now() - timedelta(days=7)
    attention = {"no_party": 0, "flagged": 0, "duplicates": 0, "stale_unsynced": 0}
    stale = []

    for key, bill_model, analysed, party_field in ALL_SIDES:
        bills = _visible(bill_model).filter(organization=organization)

        attention["duplicates"] += bills.filter(is_duplicate=True).count()

        analysed_rows = analysed.objects.filter(
            organization=organization, selected_bill__in=bills,
        )
        attention["no_party"] += analysed_rows.filter(
            **{f"{party_field}__isnull": True}
        ).count()

        for b in bills.exclude(analysed_data=None):
            if (b.analysed_data or {}).get("_extraction_checks"):
                attention["flagged"] += 1

        # Analysed a week ago and still not synced: the queue that
        # quietly grows while everything looks fine.
        old = bills.filter(created_at__lt=week_ago).exclude(
            status__iexact="Synced"
        ).exclude(status__iexact="Draft")
        attention["stale_unsynced"] += old.count()
        for b in old.order_by("created_at")[:5]:
            stale.append({
                "module": key,
                "name": b.bill_munshi_name,
                "status": b.status,
                "days": (timezone.now() - b.created_at).days,
            })

    # ---- who we trade with most --------------------------------------
    def top(analysed, party_field, limit=6):
        rows = (
            analysed.objects.filter(organization=organization)
            .exclude(**{f"{party_field}__isnull": True})
            .values(f"{party_field}__name")
            .annotate(amount=Sum("total"), count=Count("id"))
            .order_by("-amount")[:limit]
        )
        return [
            {"name": r[f"{party_field}__name"], "amount": _f(r["amount"]),
             "count": r["count"]}
            for r in rows
        ]

    return Response({
        "gst": {
            "input": inp,
            "output": outp,
            "net": net,
            "position": "payable" if net > 0 else "credit",
        },
        "attention": attention,
        "stale_unsynced": sorted(stale, key=lambda r: -r["days"])[:8],
        "top_vendors": top(TallyVendorAnalyzedBill, "vendor"),
        "top_customers": top(TallySalesAnalyzedBill, "customer"),
    })
