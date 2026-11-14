# apps/module/zoho/views/sync.py
"""
Zoho data sync views – vendors, chart of accounts, taxes, TDS/TCS.
"""
import logging
from decimal import Decimal, InvalidOperation

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common import roles
from apps.common.permissions import HasOrgPermission, requires

from apps.common.pagination import DefaultPagination
from ..models import (
    ZohoVendor, ZohoCustomer, ZohoChartOfAccount, ZohoTaxes, ZohoTdsTcs,
    ZohoVendorCredit,
)
from ..serializers.settings import (
    ZohoVendorSerializer,
    ZohoVendorCreditsSerializer,
    ZohoCustomerSerializer,
    ZohoChartOfAccountSerializer,
    ZohoTaxesSerializer,
    ZohoTdsTcsSerializer,
)
from .helpers import (
    ZohoApiError, fetch_all_zoho_pages, get_organization_from_request,
    get_zoho_credentials, make_zoho_api_request,
)

logger = logging.getLogger(__name__)


def _upsert_master(model, organization, lookup, defaults):
    """update_or_create that survives a duplicated master row.

    None of these tables carries a unique constraint on its Zoho id, so
    two syncs running at once — an impatient double-click is enough —
    can each insert the same row. From then on ``update_or_create``
    raises MultipleObjectsReturned and the sync is broken for good,
    long after the double-click is forgotten.

    Adding the constraint is the real fix, but it cannot be done from
    here: every one of these masters is the target of ``CASCADE``
    foreign keys from analysed bill data, so de-duplicating a live
    database would take the bills with it. That needs a decision and a
    migration of its own.

    Until then: keep the oldest row, update it, and say in the log that
    there are duplicates to clean up.

    Returns True if a row was created.
    """
    rows = list(model.objects.filter(organization=organization, **lookup)[:2])
    if len(rows) > 1:
        logger.warning(
            "%s has duplicate rows for %s in organisation %s — updating the "
            "oldest. These need de-duplicating.",
            model.__name__, lookup, organization.id,
        )
    if rows:
        row = rows[0]
        changed = [
            field for field, value in defaults.items()
            if getattr(row, field) != value
        ]
        if changed:
            for field, value in defaults.items():
                setattr(row, field, value)
            row.save(update_fields=changed)
        return False

    model.objects.create(organization=organization, **lookup, **defaults)
    return True


def _sync_detail(what, created, updated, truncated=False):
    """One line an operator can read without opening the logs.

    A re-sync of an unchanged list used to report "synced 0",
    which reads as a failure. Separating created from updated says
    the list was read and nothing had changed.
    """
    if not created and not updated:
        return (
            f"Zoho returned no {what}. If you expect some, check they "
            f"exist in this Zoho Books organisation."
        )
    parts = []
    if created:
        parts.append(f"{created} new")
    if updated:
        parts.append(f"{updated} updated")
    detail = f"Successfully synced {what}: {', '.join(parts)}"
    if truncated:
        detail += (
            ". Zoho has more than this sync can page through — "
            "some are missing."
        )
    return detail
def _sync_error_response(exc, what):
    """Turn a failed sync into something the operator can act on.

    Every handler used to return ``f"Sync failed: {str(e)}"``. For a
    Zoho rejection that produced "Sync failed: 400 Client Error: for
    url: https://www.zohoapis.in/...?organization_id=..." — no cause,
    and an internal URL on the operator's screen. ``ZohoApiError``
    carries Zoho's own message; anything else is ours and stays
    generic, because an unexpected traceback is not a message for a
    user.
    """
    if isinstance(exc, ZohoApiError):
        logger.warning("%s sync failed: %s", what, exc.message)
        return Response(
            {
                "detail": f"Could not sync {what}. {exc.message}",
                "zoho_code": exc.zoho_code,
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )

    logger.exception("%s sync failed", what)
    return Response(
        {"detail": f"Could not sync {what}. Check the server logs for details."},
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )



# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------


@extend_schema(responses=ZohoVendorSerializer(many=True), tags=["Zoho Ops"], methods=["GET"])
@requires(roles.VIEW_BILLS)
@api_view(["GET"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def vendors_list_view(request, org_id):
    """List all vendors for the organization with pagination."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    vendors = ZohoVendor.objects.filter(organization=organization).order_by("companyName")
    paginator = DefaultPagination()
    paginated = paginator.paginate_queryset(vendors, request)
    if paginated is not None:
        return paginator.get_paginated_response(ZohoVendorSerializer(paginated, many=True).data)

    return Response({"count": vendors.count(), "next": None, "previous": None, "results": ZohoVendorSerializer(vendors, many=True).data})


@extend_schema(responses={"200": {"detail": "Vendors synced successfully"}}, tags=["Zoho Ops"], methods=["POST"])
@requires(roles.MANAGE_MASTERS)
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def vendors_sync_view(request, org_id):
    """Sync vendors from Zoho Books."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
        # Filter on Zoho's side. Pulling the whole contact book and
        # discarding customers here means a workspace whose first 200
        # contacts are all customers syncs no vendors at all.
        contacts, truncated = fetch_all_zoho_pages(
            credentials, "contacts?contact_type=vendor", "contacts",
        )

        created_count = updated_count = 0
        for contact in contacts:
            if contact.get("contact_type") != "vendor":
                continue
            created = _upsert_master(
                ZohoVendor, organization,
                {"contactId": contact["contact_id"]},
                {
                    # A vendor trading under a personal name has no
                    # company_name, and a blank here is a row nobody can
                    # pick and no bill can match.
                    "companyName": (
                        contact.get("company_name")
                        or contact.get("contact_name")
                        or ""
                    )[:100],
                    "gstNo": (contact.get("gst_no") or "")[:30],
                    "gst_treatment": contact.get("gst_treatment") or "",
                },
            )
            if created:
                created_count += 1
            else:
                updated_count += 1

        return Response({
            "detail": _sync_detail("vendors", created_count, updated_count, truncated),
            "synced_count": created_count,
            "updated_count": updated_count,
            "total_vendors": created_count + updated_count,
            "truncated": truncated,
        })
    except Exception as e:
        logger.error(f"Vendors sync failed: {str(e)}")
        return _sync_error_response(e, "vendors")


# ---------------------------------------------------------------------------
# Vendor credits
#
# The page, the route, the model and the serializer for these all
# existed; the two views did not, so the screen called
# ``zoho/<org>/vendor-credits/`` and got a 404 every time. It was hidden
# from the menu rather than finished.
# ---------------------------------------------------------------------------


@extend_schema(responses=ZohoVendorCreditsSerializer(many=True), tags=["Zoho Ops"], methods=["GET"])
@requires(roles.VIEW_BILLS)
@api_view(["GET"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def vendor_credits_list_view(request, org_id):
    """List the vendor credit notes synced from Zoho Books."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    credits = (
        ZohoVendorCredit.objects
        .filter(organization=organization)
        .order_by("-created_at")
    )
    paginator = DefaultPagination()
    paginated = paginator.paginate_queryset(credits, request)
    if paginated is not None:
        return paginator.get_paginated_response(
            ZohoVendorCreditsSerializer(paginated, many=True).data
        )
    return Response({
        "count": credits.count(), "next": None, "previous": None,
        "results": ZohoVendorCreditsSerializer(credits, many=True).data,
    })


@extend_schema(responses={"200": {"detail": "Vendor credits synced successfully"}},
               tags=["Zoho Ops"], methods=["POST"])
@requires(roles.MANAGE_MASTERS)
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def vendor_credits_sync_view(request, org_id):
    """Pull vendor credit notes from Zoho Books.

    Same shape as every other master sync here: page through the Zoho
    collection, upsert on the Zoho id, report what changed.
    """
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
        rows, truncated = fetch_all_zoho_pages(
            credentials, "vendorcredits", "vendorcredits",
        )

        created_count = updated_count = 0
        for row in rows:
            credit_id = row.get("vendor_credit_id")
            if not credit_id:
                continue
            created = _upsert_master(
                ZohoVendorCredit, organization,
                {"vendor_credit_id": str(credit_id)},
                {
                    "vendor_id": str(row.get("vendor_id") or ""),
                    "vendor_name": row.get("vendor_name") or "",
                    "vendor_credit_number": row.get("vendor_credit_number") or "",
                },
            )
            if created:
                created_count += 1
            else:
                updated_count += 1

        message = f"Vendor credits synced: {created_count} added, {updated_count} updated"
        if truncated:
            message += " (more pages remain — run the sync again)"
        return Response({"detail": message,
                         "created": created_count, "updated": updated_count})
    except ZohoApiError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


@extend_schema(responses=ZohoCustomerSerializer(many=True), tags=["Zoho Ops"], methods=["GET"])
@requires(roles.VIEW_BILLS)
@api_view(["GET"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def customers_list_view(request, org_id):
    """List all customers for the organization with pagination."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    customers = ZohoCustomer.objects.filter(organization=organization).order_by("companyName")
    paginator = DefaultPagination()
    paginated = paginator.paginate_queryset(customers, request)
    if paginated is not None:
        return paginator.get_paginated_response(ZohoCustomerSerializer(paginated, many=True).data)

    return Response({"count": customers.count(), "next": None, "previous": None, "results": ZohoCustomerSerializer(customers, many=True).data})


@extend_schema(responses={"200": {"detail": "Customers synced successfully"}}, tags=["Zoho Ops"], methods=["POST"])
@requires(roles.MANAGE_MASTERS)
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def customers_sync_view(request, org_id):
    """Sync customers from Zoho Books.

    Same ``contacts`` endpoint the vendor sync reads — Zoho keeps both
    sides in one collection and separates them by ``contact_type``.
    """
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
        contacts, truncated = fetch_all_zoho_pages(
            credentials, "contacts?contact_type=customer", "contacts",
        )

        created_count = updated_count = 0
        for contact in contacts:
            if contact.get("contact_type") != "customer":
                continue
            created = _upsert_master(
                ZohoCustomer, organization,
                {"contactId": contact["contact_id"]},
                {
                    # Individual customers have no company_name; falling back
                    # to contact_name keeps the picker from showing blanks.
                    "companyName": (
                        contact.get("company_name")
                        or contact.get("contact_name")
                        or ""
                    )[:100],
                    "gstNo": (contact.get("gst_no") or "")[:30],
                    "gst_treatment": contact.get("gst_treatment") or "",
                    "place_of_supply": (contact.get("place_of_contact") or "")[:10],
                },
            )
            if created:
                created_count += 1
            else:
                updated_count += 1

        return Response({
            "detail": _sync_detail("customers", created_count, updated_count, truncated),
            "synced_count": created_count,
            "updated_count": updated_count,
            "total_customers": created_count + updated_count,
            "truncated": truncated,
        })
    except Exception as e:
        logger.error(f"Customers sync failed: {str(e)}")
        return _sync_error_response(e, "customers")


# ---------------------------------------------------------------------------
# Chart of Accounts
# ---------------------------------------------------------------------------


@extend_schema(responses=ZohoChartOfAccountSerializer(many=True), tags=["Zoho Ops"], methods=["GET"])
@requires(roles.VIEW_BILLS)
@api_view(["GET"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def chart_of_accounts_list_view(request, org_id):
    """List all chart of accounts for the organization with pagination."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    accounts = ZohoChartOfAccount.objects.filter(organization=organization).order_by("accountName")
    paginator = DefaultPagination()
    paginated = paginator.paginate_queryset(accounts, request)
    if paginated is not None:
        return paginator.get_paginated_response(ZohoChartOfAccountSerializer(paginated, many=True).data)

    return Response({"results": ZohoChartOfAccountSerializer(accounts, many=True).data})


@extend_schema(responses={"200": {"detail": "Chart of accounts synced successfully"}}, tags=["Zoho Ops"], methods=["POST"])
@requires(roles.MANAGE_MASTERS)
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def chart_of_accounts_sync_view(request, org_id):
    """Sync chart of accounts from Zoho Books."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)

        if not credentials.is_connected:
            return Response(
                {"detail": "Zoho credentials not connected. Please authenticate first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        accounts, coa_truncated = fetch_all_zoho_pages(
            credentials, "chartofaccounts", "chartofaccounts",
        )

        synced_count = 0
        updated_count = 0
        for account in accounts:
            created = _upsert_master(
                ZohoChartOfAccount, organization,
                {"accountId": account["account_id"]},
                {"accountName": (account.get("account_name") or "")[:100]},
            )
            if created:
                synced_count += 1
            else:
                updated_count += 1

        return Response(
            {
                "detail": _sync_detail(
                    "chart of accounts", synced_count, updated_count, coa_truncated,
                ),
                "synced_count": synced_count,
                "updated_count": updated_count,
                "total_accounts": len(accounts),
                "truncated": coa_truncated,
            }
        )

    except ValueError as e:
        return Response({"detail": f"Authentication error: {str(e)}"}, status=status.HTTP_401_UNAUTHORIZED)
    except Exception as e:
        logger.error(f"Chart of accounts sync failed: {str(e)}")
        return _sync_error_response(e, "the chart of accounts")


# ---------------------------------------------------------------------------
# Taxes
# ---------------------------------------------------------------------------


@extend_schema(responses=ZohoTaxesSerializer(many=True), tags=["Zoho Ops"], methods=["GET"])
@requires(roles.VIEW_BILLS)
@api_view(["GET"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def taxes_list_view(request, org_id):
    """List all taxes for the organization with pagination."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    taxes = ZohoTaxes.objects.filter(organization=organization).order_by("taxName")
    paginator = DefaultPagination()
    paginated = paginator.paginate_queryset(taxes, request)
    if paginated is not None:
        return paginator.get_paginated_response(ZohoTaxesSerializer(paginated, many=True).data)

    return Response({"results": ZohoTaxesSerializer(taxes, many=True).data})


@extend_schema(responses={"200": {"detail": "Taxes synced successfully"}}, tags=["Zoho Ops"], methods=["POST"])
@requires(roles.MANAGE_MASTERS)
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def taxes_sync_view(request, org_id):
    """Sync taxes from Zoho Books."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
        zoho_data = make_zoho_api_request(credentials, "settings/taxes")
        zoho_taxes = zoho_data.get("taxes", [])

        def _pct(tax):
            try:
                return Decimal(str(tax.get("tax_percentage")))
            except (TypeError, ValueError, InvalidOperation):
                return None

        existing = {
            t.taxId: t
            for t in ZohoTaxes.objects.filter(
                taxId__in=[t["tax_id"] for t in zoho_taxes], organization=organization
            )
        }

        new_taxes, updated = [], []
        for t in zoho_taxes:
            row = existing.get(t["tax_id"])
            if row is None:
                new_taxes.append(ZohoTaxes(
                    taxId=t["tax_id"], taxName=t["tax_name"],
                    percentage=_pct(t), organization=organization,
                ))
                continue
            # Refresh rows that already exist: they were created before
            # the percentage was stored, and a re-sync is how they get
            # it. Rates also change — GST 2.0 moved several slabs.
            pct, name = _pct(t), t["tax_name"]
            if row.percentage != pct or row.taxName != name:
                row.percentage, row.taxName = pct, name
                updated.append(row)

        if new_taxes:
            ZohoTaxes.objects.bulk_create(new_taxes)
        if updated:
            ZohoTaxes.objects.bulk_update(updated, ["percentage", "taxName"])

        return Response({
            "detail": (
                f"Successfully synced {len(new_taxes)} taxes"
                + (f", refreshed {len(updated)}" if updated else "")
            ),
            "synced_count": len(new_taxes),
            "updated_count": len(updated),
        })
    except Exception as e:
        logger.error(f"Taxes sync failed: {str(e)}")
        return _sync_error_response(e, "taxes")


# ---------------------------------------------------------------------------
# TDS / TCS
# ---------------------------------------------------------------------------


@extend_schema(
    responses=ZohoTdsTcsSerializer(many=True),
    tags=["Zoho Ops"],
    methods=["GET"],
    parameters=[
        {
            "name": "tax_type",
            "in": "query",
            "description": "Filter by tax type (TDS or TCS)",
            "required": False,
            "schema": {"type": "string", "enum": ["TDS", "TCS"]},
        }
    ],
)
@requires(roles.VIEW_BILLS)
@api_view(["GET"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def tds_tcs_list_view(request, org_id):
    """List all TDS/TCS for the organization with pagination and optional filtering."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    tds_tcs = ZohoTdsTcs.objects.filter(organization=organization)

    tax_type = request.query_params.get("tax_type")
    if tax_type and tax_type.upper() in ("TDS", "TCS"):
        tds_tcs = tds_tcs.filter(taxType=tax_type.upper())

    tds_tcs = tds_tcs.order_by("taxName")

    paginator = DefaultPagination()
    paginated = paginator.paginate_queryset(tds_tcs, request)
    if paginated is not None:
        return paginator.get_paginated_response(ZohoTdsTcsSerializer(paginated, many=True).data)

    return Response({"count": tds_tcs.count(), "next": None, "previous": None, "results": ZohoTdsTcsSerializer(tds_tcs, many=True).data})


@extend_schema(responses={"200": {"detail": "TDS/TCS synced successfully"}}, tags=["Zoho Ops"], methods=["POST"])
@requires(roles.MANAGE_MASTERS)
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def tds_tcs_sync_view(request, org_id):
    """Sync TDS/TCS taxes from Zoho Books."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)

        def _load(endpoint, tax_type):
            """Insert new rows and refresh the ones already held.

            Refreshing matters as much as inserting. A TDS section's
            rate changes by notification — 194C has moved more than
            once — and a row that only ever gets written on first sight
            keeps the old rate for ever. Nothing reports that: the
            deduction is simply computed at the wrong percentage and
            the challan is short.
            """
            payload = make_zoho_api_request(credentials, endpoint)
            rows = payload.get("taxes") or []

            existing = {
                r.taxId: r
                for r in ZohoTdsTcs.objects.filter(
                    taxId__in=[t["tax_id"] for t in rows if t.get("tax_id")],
                    taxType=tax_type, organization=organization,
                )
            }

            created, updated = [], []
            for t in rows:
                tax_id = t.get("tax_id")
                if not tax_id:
                    continue
                name = str(t.get("tax_name") or "")[:100]
                pct = str(t.get("tax_percentage", 0))[:100]
                row = existing.get(tax_id)
                if row is None:
                    created.append(ZohoTdsTcs(
                        taxId=tax_id, taxName=name, taxPercentage=pct,
                        taxType=tax_type, organization=organization,
                    ))
                elif row.taxName != name or row.taxPercentage != pct:
                    row.taxName, row.taxPercentage = name, pct
                    updated.append(row)

            if created:
                ZohoTdsTcs.objects.bulk_create(created)
            if updated:
                ZohoTdsTcs.objects.bulk_update(updated, ["taxName", "taxPercentage"])
            return len(created), len(updated)

        tds_new, tds_updated = _load("settings/taxes?is_tds_request=true", "TDS")
        tcs_new, tcs_updated = _load(
            "settings/taxes?is_tcs_request=true&filter_by=Taxes.All", "TCS",
        )

        return Response(
            {
                "detail": _sync_detail(
                    "TDS/TCS", tds_new + tcs_new, tds_updated + tcs_updated,
                ),
                "synced_count": tds_new + tcs_new,
                "updated_count": tds_updated + tcs_updated,
                "tds_count": tds_new,
                "tcs_count": tcs_new,
            }
        )

    except Exception as e:
        logger.error(f"TDS/TCS sync failed: {str(e)}")
        return _sync_error_response(e, "TDS/TCS")
