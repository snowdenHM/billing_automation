# apps/dashboard/views/zoho.py
"""Zoho dashboard analytics views — thin wrappers around generic helpers."""

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from apps.module.zoho.models import (
    VendorBill,
    JournalBill,
    VendorZohoBill,
    JournalZohoBill,
    ZohoVendor,
)
from ..helpers import build_overview_response, build_funnel_response, build_usage_response
from ..serializers import (
    ZohoOverviewResponseSerializer,
    ZohoFunnelResponseSerializer,
    ZohoUsageResponseSerializer,
    ErrorResponseSerializer,
)


@extend_schema(
    summary="Get Zoho overview statistics",
    description="Returns comprehensive overview of Zoho bills, vendors, and processing status",
    tags=["Zoho Dashboard"],
    responses={200: ZohoOverviewResponseSerializer, 404: ErrorResponseSerializer},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def zoho_overview_view(request, org_id):
    return build_overview_response(
        org_id,
        vendor_bill_model=VendorBill,
        expense_bill_model=JournalBill,
        analyzed_vendor_model=VendorZohoBill,
        analyzed_expense_model=JournalZohoBill,
        counter_model=ZohoVendor,
    )


@extend_schema(
    summary="Get Zoho processing funnel data",
    description="Returns funnel analysis of bill processing stages",
    tags=["Zoho Dashboard"],
    responses={200: ZohoFunnelResponseSerializer, 404: ErrorResponseSerializer},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def zoho_funnel_view(request, org_id):
    return build_funnel_response(
        org_id,
        vendor_bill_model=VendorBill,
        expense_bill_model=JournalBill,
    )


@extend_schema(
    summary="Get Zoho usage statistics",
    description="Returns usage statistics and activity metrics",
    tags=["Zoho Dashboard"],
    responses={200: ZohoUsageResponseSerializer, 404: ErrorResponseSerializer},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def zoho_usage_view(request, org_id):
    return build_usage_response(
        org_id,
        vendor_bill_model=VendorBill,
        expense_bill_model=JournalBill,
        updated_at_field="update_at",
    )
