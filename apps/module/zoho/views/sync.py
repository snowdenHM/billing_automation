# apps/module/zoho/views/sync.py
"""
Zoho data sync views – vendors, chart of accounts, taxes, TDS/TCS.
"""
import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.pagination import DefaultPagination
from ..models import ZohoVendor, ZohoChartOfAccount, ZohoTaxes, ZohoTdsTcs
from ..serializers.settings import (
    ZohoVendorSerializer,
    ZohoChartOfAccountSerializer,
    ZohoTaxesSerializer,
    ZohoTdsTcsSerializer,
)
from .helpers import get_organization_from_request, get_zoho_credentials, make_zoho_api_request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------


@extend_schema(responses=ZohoVendorSerializer(many=True), tags=["Zoho Ops"], methods=["GET"])
@api_view(["GET"])
@permission_classes([IsAuthenticated])
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
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def vendors_sync_view(request, org_id):
    """Sync vendors from Zoho Books."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
        zoho_data = make_zoho_api_request(credentials, "contacts")

        synced_count = 0
        for contact in zoho_data.get("contacts", []):
            if contact.get("contact_type") == "vendor":
                _, created = ZohoVendor.objects.update_or_create(
                    organization=organization,
                    contactId=contact["contact_id"],
                    defaults={
                        "companyName": contact.get("company_name", ""),
                        "gstNo": contact.get("gst_no", ""),
                        "gst_treatment": contact.get("gst_treatment", ""),
                    },
                )
                if created:
                    synced_count += 1

        return Response({"detail": f"Successfully synced {synced_count} vendors", "synced_count": synced_count})
    except Exception as e:
        return Response({"detail": f"Sync failed: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ---------------------------------------------------------------------------
# Chart of Accounts
# ---------------------------------------------------------------------------


@extend_schema(responses=ZohoChartOfAccountSerializer(many=True), tags=["Zoho Ops"], methods=["GET"])
@api_view(["GET"])
@permission_classes([IsAuthenticated])
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
@api_view(["POST"])
@permission_classes([IsAuthenticated])
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

        zoho_data = make_zoho_api_request(credentials, "chartofaccounts")

        synced_count = 0
        updated_count = 0
        for account in zoho_data.get("chartofaccounts", []):
            _, created = ZohoChartOfAccount.objects.update_or_create(
                organization=organization,
                accountId=account["account_id"],
                defaults={"accountName": account.get("account_name", "")},
            )
            if created:
                synced_count += 1
            else:
                updated_count += 1

        return Response(
            {
                "detail": f"Successfully synced chart of accounts: {synced_count} new, {updated_count} updated",
                "synced_count": synced_count,
                "updated_count": updated_count,
                "total_accounts": len(zoho_data.get("chartofaccounts", [])),
            }
        )

    except ValueError as e:
        return Response({"detail": f"Authentication error: {str(e)}"}, status=status.HTTP_401_UNAUTHORIZED)
    except Exception as e:
        logger.error(f"Chart of accounts sync failed: {str(e)}")
        return Response({"detail": f"Sync failed: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ---------------------------------------------------------------------------
# Taxes
# ---------------------------------------------------------------------------


@extend_schema(responses=ZohoTaxesSerializer(many=True), tags=["Zoho Ops"], methods=["GET"])
@api_view(["GET"])
@permission_classes([IsAuthenticated])
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
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def taxes_sync_view(request, org_id):
    """Sync taxes from Zoho Books."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
        zoho_data = make_zoho_api_request(credentials, "settings/taxes")
        zoho_taxes = zoho_data.get("taxes", [])

        existing_ids = set(
            ZohoTaxes.objects.filter(
                taxId__in=[t["tax_id"] for t in zoho_taxes], organization=organization
            ).values_list("taxId", flat=True)
        )

        new_taxes = [
            ZohoTaxes(taxId=t["tax_id"], taxName=t["tax_name"], organization=organization)
            for t in zoho_taxes
            if t["tax_id"] not in existing_ids
        ]

        if new_taxes:
            ZohoTaxes.objects.bulk_create(new_taxes)

        return Response({"detail": f"Successfully synced {len(new_taxes)} taxes", "synced_count": len(new_taxes)})
    except Exception as e:
        logger.error(f"Taxes sync failed: {str(e)}")
        return Response({"detail": f"Sync failed: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
@api_view(["GET"])
@permission_classes([IsAuthenticated])
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
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def tds_tcs_sync_view(request, org_id):
    """Sync TDS/TCS taxes from Zoho Books."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        credentials = get_zoho_credentials(organization)
        synced_count = 0

        # TDS
        tds_data = make_zoho_api_request(credentials, "settings/taxes?is_tds_request=true")
        tds_taxes = tds_data.get("taxes", [])
        existing_tds = set(
            ZohoTdsTcs.objects.filter(taxId__in=[t["tax_id"] for t in tds_taxes], taxType="TDS", organization=organization).values_list("taxId", flat=True)
        )
        new_tds = [
            ZohoTdsTcs(taxId=t["tax_id"], taxName=t["tax_name"], taxPercentage=t.get("tax_percentage", 0), taxType="TDS", organization=organization)
            for t in tds_taxes
            if t["tax_id"] not in existing_tds
        ]

        # TCS
        tcs_data = make_zoho_api_request(credentials, "settings/taxes?is_tcs_request=true&filter_by=Taxes.All")
        tcs_taxes = tcs_data.get("taxes", [])
        existing_tcs = set(
            ZohoTdsTcs.objects.filter(taxId__in=[t["tax_id"] for t in tcs_taxes], taxType="TCS", organization=organization).values_list("taxId", flat=True)
        )
        new_tcs = [
            ZohoTdsTcs(taxId=t["tax_id"], taxName=t["tax_name"], taxPercentage=t.get("tax_percentage", 0), taxType="TCS", organization=organization)
            for t in tcs_taxes
            if t["tax_id"] not in existing_tcs
        ]

        if new_tds:
            ZohoTdsTcs.objects.bulk_create(new_tds)
            synced_count += len(new_tds)
        if new_tcs:
            ZohoTdsTcs.objects.bulk_create(new_tcs)
            synced_count += len(new_tcs)

        return Response(
            {
                "detail": f"Successfully synced {synced_count} TDS/TCS taxes",
                "synced_count": synced_count,
                "tds_count": len(new_tds),
                "tcs_count": len(new_tcs),
            }
        )

    except Exception as e:
        logger.error(f"TDS/TCS sync failed: {str(e)}")
        return Response({"detail": f"Sync failed: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
