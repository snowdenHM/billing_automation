from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum, F, Q, Value, DateField
from django.db.models.functions import TruncDay, TruncWeek, TruncMonth, Cast
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes

from apps.organizations.models import Organization
# ZOHO models
from apps.module.zoho.models import (
    VendorBill, ExpenseBill,
    VendorZohoBill, ExpenseZohoBill,
    VendorZohoProduct, ExpenseZohoProduct,
    ZohoVendor, ZohoChartOfAccount,
    ZohoTaxes, ZohoTdsTcs, ZohoCredentials
)
# TALLY models (optional aggregation)
from apps.module.tally.models import (
    TallyVendorBill, TallyExpenseBill,
    TallyVendorAnalyzedBill, TallyExpenseAnalyzedBill,
    TallyExpenseAnalyzedProduct
)

from .serializers import (
    OverviewSerializer, TimeseriesResponseSerializer, FunnelResponseSerializer,
    TopVendorsResponseSerializer, TaxesSummarySerializer, ExpenseSummarySerializer,
    PendingResponseSerializer, IntegrationsHealthSerializer, UsageSerializer
)


# -----------------------
# Helpers
# -----------------------
def _user_has_org_access(user, org: Organization) -> bool:
    """
    Conservative org access check. Adjust to your actual Organization relations if needed.
    """
    if not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False):
        return True
    # common patterns – adapt to your model fields
    if hasattr(org, "owner_id") and org.owner_id == user.id:
        return True
    if hasattr(org, "admins") and org.admins.filter(id=user.id).exists():
        return True
    if hasattr(org, "members") and org.members.filter(id=user.id).exists():
        return True
    # fallback: if there is a generic users M2M
    if hasattr(org, "users") and org.users.filter(id=user.id).exists():
        return True
    return False


def _org_from_path_or_403(request, org_id):
    try:
        org = Organization.objects.get(id=org_id)
    except Organization.DoesNotExist:
        return None, Response({"detail": "Organization not found."}, status=404)

    if not _user_has_org_access(request.user, org):
        return None, Response({"detail": "Forbidden for this organization."}, status=403)

    return org, None


def _date_range_from_query(request):
    """Return (date_from, date_to). Defaults to last 30 days."""
    try:
        date_from = request.query_params.get("from")
        date_to = request.query_params.get("to")
        if date_from:
            date_from = timezone.datetime.fromisoformat(date_from).date()
        if date_to:
            date_to = timezone.datetime.fromisoformat(date_to).date()
    except Exception:
        date_from = None
        date_to = None

    if not date_to:
        date_to = timezone.now().date()
    if not date_from:
        date_from = date_to - timedelta(days=30)
    return date_from, date_to


def _daterange_filter(field="created_at", start=None, end=None):
    q = Q()
    if start:
        q &= Q(**{f"{field}__date__gte": start})
    if end:
        q &= Q(**{f"{field}__date__lte": end})
    return q


# -----------------------
# Views (org-scoped)
# -----------------------
class OverviewView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = OverviewSerializer

    def get(self, request, org_id):
        org, err = _org_from_path_or_403(request, org_id)
        if err:
            return err
        dfrom, dto = _date_range_from_query(request)

        vb_qs = VendorBill.objects.filter(organization=org).filter(_daterange_filter("created_at", dfrom, dto))
        eb_qs = ExpenseBill.objects.filter(organization=org).filter(_daterange_filter("created_at", dfrom, dto))

        uploaded = vb_qs.count() + eb_qs.count()
        analysed = vb_qs.filter(status="Analysed").count() + eb_qs.filter(status="Analysed").count()
        verified = vb_qs.filter(status="Verified").count() + eb_qs.filter(status="Verified").count()
        synced = vb_qs.filter(status="Synced").count() + eb_qs.filter(status="Synced").count()

        sync_rate = float(synced / uploaded) if uploaded else 0.0

        def avg_secs(qs, status_value):
            qs = qs.filter(status=status_value).exclude(update_at=None)
            deltas = [(b.update_at - b.created_at).total_seconds() for b in qs[:500]]
            return sum(deltas) / len(deltas) if deltas else None

        avg_a_v = [avg_secs(vb_qs, "Analysed"), avg_secs(eb_qs, "Analysed")]
        avg_v_v = [avg_secs(vb_qs, "Verified"), avg_secs(eb_qs, "Verified")]

        avg_analyse = (
            sum([v for v in avg_a_v if v is not None]) / max(1, len([v for v in avg_a_v if v is not None]))
            if any(avg_a_v) else None
        )
        avg_verify = (
            sum([v for v in avg_v_v if v is not None]) / max(1, len([v for v in avg_v_v if v is not None]))
            if any(avg_v_v) else None
        )

        data = {
            "totals": {
                "vendor_bills": vb_qs.count(),
                "expense_bills": eb_qs.count(),
                "analysed": analysed,
                "verified": verified,
                "synced": synced,
            },
            "sync_rate": round(sync_rate, 4),
            "avg_analyse_time_sec": round(avg_analyse, 2) if avg_analyse is not None else None,
            "avg_verify_time_sec": round(avg_verify, 2) if avg_verify is not None else None,
        }
        return Response(OverviewSerializer(data).data)


class TimeseriesView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TimeseriesResponseSerializer

    def get(self, request, org_id):
        org, err = _org_from_path_or_403(request, org_id)
        if err:
            return err
        dfrom, dto = _date_range_from_query(request)

        interval = (request.query_params.get("interval") or "day").lower()
        trunc_fn = TruncDay
        if interval == "week":
            trunc_fn = TruncWeek
        elif interval == "month":
            trunc_fn = TruncMonth

        points = defaultdict(lambda: {"uploaded": 0, "analysed": 0, "verified": 0, "synced": 0})

        for model in (VendorBill, ExpenseBill):
            base = model.objects.filter(organization=org).filter(_daterange_filter("created_at", dfrom, dto))
            for row in base.annotate(d=trunc_fn("created_at")).values("d").annotate(c=Count("id")).order_by("d"):
                k = row["d"].date()
                points[k]["uploaded"] += row["c"]
            for st in ["Analysed", "Verified", "Synced"]:
                qs = base.filter(status=st).annotate(d=trunc_fn("update_at")).values("d").annotate(c=Count("id")).order_by("d")
                for row in qs:
                    k = row["d"].date()
                    points[k][st.lower()] += row["c"]

        series = [{"date": d, **v} for d, v in sorted(points.items(), key=lambda x: x[0])]
        return Response(TimeseriesResponseSerializer({"series": series}).data)


class FunnelView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = FunnelResponseSerializer

    def get(self, request, org_id):
        org, err = _org_from_path_or_403(request, org_id)
        if err:
            return err
        dfrom, dto = _date_range_from_query(request)

        def counts(model):
            qs = model.objects.filter(organization=org).filter(_daterange_filter("created_at", dfrom, dto))
            return {
                "uploaded": qs.count(),
                "analysed": qs.filter(status="Analysed").count(),
                "verified": qs.filter(status="Verified").count(),
                "synced": qs.filter(status="Synced").count(),
            }

        a = counts(VendorBill)
        b = counts(ExpenseBill)
        agg = {k: a.get(k, 0) + b.get(k, 0) for k in ("uploaded", "analysed", "verified", "synced")}
        funnel = [{"stage": k, "count": agg[k]} for k in ("uploaded", "analysed", "verified", "synced")]
        dropoffs = {
            "uploaded→analysed": max(0, agg["uploaded"] - agg["analysed"]),
            "analysed→verified": max(0, agg["analysed"] - agg["verified"]),
            "verified→synced": max(0, agg["verified"] - agg["synced"]),
        }
        return Response(FunnelResponseSerializer({"funnel": funnel, "dropoffs": dropoffs}).data)


class TopVendorsView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TopVendorsResponseSerializer

    def get(self, request, org_id):
        org, err = _org_from_path_or_403(request, org_id)
        if err:
            return err
        dfrom, dto = _date_range_from_query(request)
        limit = int(request.query_params.get("limit", 10))

        cleaned = []

        # Zoho vendor bills (totals are CharField → cannot reliably Sum; keep count, total_amount=0)
        zb = (
            VendorZohoBill.objects
            .filter(organization=org)
            .filter(_daterange_filter("created_at", dfrom, dto))
            .select_related("vendor")
            .values(name=F("vendor__companyName"))
            .annotate(count=Count("id"))
        )
        for row in zb:
            cleaned.append({
                "name": row["name"] or "Unknown Vendor",
                "count": row["count"],
                "total_amount": Decimal("0.00"),
            })

        # Tally vendor analyzed bills (Decimal 'total')
        tb = (
            TallyVendorAnalyzedBill.objects
            .filter(organization=org)
            .filter(_daterange_filter("created_at", dfrom, dto))
            .select_related("vendor")
            .values(name=F("vendor__name"))
            .annotate(count=Count("id"), total_amount=Sum("total"))
        )
        for row in tb:
            cleaned.append({
                "name": row["name"] or "Unknown Vendor",
                "count": row["count"] or 0,
                "total_amount": row["total_amount"] or Decimal("0.00"),
            })

        agg = defaultdict(lambda: {"count": 0, "total_amount": Decimal("0.00")})
        for r in cleaned:
            agg[r["name"]]["count"] += r["count"]
            agg[r["name"]]["total_amount"] += r["total_amount"]

        top = sorted(
            [{"name": n, "count": v["count"], "total_amount": v["total_amount"]} for n, v in agg.items()],
            key=lambda x: (x["total_amount"], x["count"]), reverse=True
        )[:limit]

        return Response(TopVendorsResponseSerializer({"vendors": top}).data)


class TaxesSummaryView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TaxesSummarySerializer

    def get(self, request, org_id):
        org, err = _org_from_path_or_403(request, org_id)
        if err:
            return err
        dfrom, dto = _date_range_from_query(request)

        def _sum_charfield_decimal(qs, field):
            total = Decimal("0.00")
            for x in qs.values_list(field, flat=True):
                try:
                    total += Decimal(str(x or "0"))
                except Exception:
                    continue
            return total

        zb = VendorZohoBill.objects.filter(organization=org).filter(_daterange_filter("created_at", dfrom, dto))
        eb = ExpenseZohoBill.objects.filter(organization=org).filter(_daterange_filter("created_at", dfrom, dto))

        mix = {
            "igst": _sum_charfield_decimal(zb, "igst") + _sum_charfield_decimal(eb, "igst"),
            "cgst": _sum_charfield_decimal(zb, "cgst") + _sum_charfield_decimal(eb, "cgst"),
            "sgst": _sum_charfield_decimal(zb, "sgst") + _sum_charfield_decimal(eb, "sgst"),
            "tds": Decimal("0.00"),
            "tcs": Decimal("0.00"),
        }
        return Response(TaxesSummarySerializer({"gst_mix": mix}).data)


class ExpenseSummaryView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ExpenseSummarySerializer

    def get(self, request, org_id):
        org, err = _org_from_path_or_403(request, org_id)
        if err:
            return err
        dfrom, dto = _date_range_from_query(request)

        rows = defaultdict(lambda: {"debit": Decimal("0.00"), "credit": Decimal("0.00")})

        for p in ExpenseZohoProduct.objects.filter(organization=org).filter(
            _daterange_filter("created_at", dfrom, dto)
        ).select_related("chart_of_accounts"):
            acct = p.chart_of_accounts.accountName if p.chart_of_accounts else "Unknown"
            try:
                amt = Decimal(str(p.amount or "0"))
            except Exception:
                amt = Decimal("0.00")
            if (p.debit_or_credit or "").lower() == "debit":
                rows[acct]["debit"] += amt
            else:
                rows[acct]["credit"] += amt

        for p in TallyExpenseAnalyzedProduct.objects.filter(organization=org).filter(
            _daterange_filter("created_at", dfrom, dto)
        ).select_related("chart_of_accounts"):
            acct = p.chart_of_accounts.name if p.chart_of_accounts else "Unknown"
            try:
                amt = Decimal(str(p.amount or "0"))
            except Exception:
                amt = Decimal("0.00")
            if (p.debit_or_credit or "").lower() == "debit":
                rows[acct]["debit"] += amt
            else:
                rows[acct]["credit"] += amt

        by_account = [{"account_name": k, "debit": v["debit"], "credit": v["credit"]} for k, v in rows.items()]
        totals = {
            "debit": sum([r["debit"] for r in by_account], Decimal("0.00")),
            "credit": sum([r["credit"] for r in by_account], Decimal("0.00")),
        }
        return Response(ExpenseSummarySerializer({"totals": totals, "by_account": by_account}).data)


class PendingView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PendingResponseSerializer

    def get(self, request, org_id):
        org, err = _org_from_path_or_403(request, org_id)
        if err:
            return err
        dfrom, dto = _date_range_from_query(request)
        limit = int(request.query_params.get("limit", 10))
        now = timezone.now()

        items = []
        for model, tname in ((VendorBill, "vendor_bill"), (ExpenseBill, "expense_bill")):
            for st in ["Analysed", "Verified"]:
                qs = (
                    model.objects.filter(organization=org, status=st)
                    .filter(_daterange_filter("update_at", dfrom, dto))
                    .order_by("-update_at")[:limit]
                )
                for b in qs:
                    age = (now - b.update_at).total_seconds() / 3600.0 if b.update_at else 0.0
                    items.append({
                        "type": tname, "id": str(b.id), "name": b.billmunshiName or "", "status": st, "age_hours": round(age, 2)
                    })

        counts = {
            "analysed": sum(1 for i in items if i["status"] == "Analysed"),
            "verified": sum(1 for i in items if i["status"] == "Verified"),
        }
        items = sorted(items, key=lambda x: x["age_hours"], reverse=True)[:limit]
        return Response(PendingResponseSerializer({"items": items, "counts": counts}).data)


class IntegrationsHealthView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = IntegrationsHealthSerializer

    def get(self, request, org_id):
        org, err = _org_from_path_or_403(request, org_id)
        if err:
            return err

        recent_analysed = VendorBill.objects.filter(
            organization=org, status="Analysed", update_at__gte=timezone.now()-timedelta(days=1)
        ).exists() or ExpenseBill.objects.filter(
            organization=org, status="Analysed", update_at__gte=timezone.now()-timedelta(days=1)
        ).exists()

        zoho_connected = ZohoCredentials.objects.filter(organization=org).exclude(refreshToken__isnull=True).exists() \
            or ZohoCredentials.objects.filter(organization=org).exclude(accessToken__isnull=True).exists()

        tally_reachable = TallyVendorBill.objects.filter(
            organization=org, status="Synced", updated_at__gte=timezone.now()-timedelta(days=1)
        ).exists() or TallyExpenseBill.objects.filter(
            organization=org, status="Synced", updated_at__gte=timezone.now()-timedelta(days=1)
        ).exists()

        data = {
            "ai_ocr": {"online": bool(recent_analysed), "latency_ms": 0},
            "zoho": {"connected": bool(zoho_connected), "last_sync": None},
            "tally_tcp": {"reachable": bool(tally_reachable), "last_push": None},
        }
        return Response(IntegrationsHealthSerializer(data).data)


class UsageView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UsageSerializer

    def get(self, request, org_id):
        org, err = _org_from_path_or_403(request, org_id)
        if err:
            return err
        dfrom, dto = _date_range_from_query(request)

        bill_uploads = VendorBill.objects.filter(organization=org).filter(_daterange_filter("created_at", dfrom, dto)).count() + \
                       ExpenseBill.objects.filter(organization=org).filter(_daterange_filter("created_at", dfrom, dto)).count()

        plan_limit = 1000  # TODO: read real plan limit from subscriptions
        api_calls = 0      # TODO: hook into API usage logs

        percent_uploads = round((bill_uploads / plan_limit) * 100.0, 2) if plan_limit else 0.0
        percent_api = 0.0

        data = {
            "plan": {"name": "Pro", "bill_upload_limit": plan_limit},
            "usage": {"bill_uploads": bill_uploads, "api_calls": api_calls},
            "percent": {"bill_uploads": percent_uploads, "api_calls": percent_api}
        }
        return Response(UsageSerializer(data).data)
