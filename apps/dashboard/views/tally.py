# apps/dashboard/views/tally.py
"""Tally dashboard analytics views — thin wrappers around generic helpers."""

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from apps.module.tally.models import (
    TallyVendorBill,
    TallyExpenseBill,
    TallyPaymentBill,
    TallyVendorAnalyzedBill,
    TallyExpenseAnalyzedBill,
    TallyPaymentAnalyzedBill,
    Ledger,
)
from ..helpers import build_overview_response, build_funnel_response, build_usage_response
from ..serializers import (
    TallyOverviewResponseSerializer,
    TallyFunnelResponseSerializer,
    TallyUsageResponseSerializer,
    ErrorResponseSerializer,
)


@extend_schema(
    summary="Get Tally overview statistics",
    description="Returns comprehensive overview of Tally bills, payment vouchers, vendors, and processing status",
    tags=["Tally Dashboard"],
    responses={200: TallyOverviewResponseSerializer, 404: ErrorResponseSerializer},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tally_overview_view(request, org_id):
    return build_overview_response(
        org_id,
        vendor_bill_model=TallyVendorBill,
        expense_bill_model=TallyExpenseBill,
        payment_bill_model=TallyPaymentBill,
        analyzed_vendor_model=TallyVendorAnalyzedBill,
        analyzed_expense_model=TallyExpenseAnalyzedBill,
        analyzed_payment_model=TallyPaymentAnalyzedBill,
        # Tally bills are trashable; keep trashed ones out of the amounts.
        analyzed_bill_field="selected_bill",
        counter_model=Ledger,
    )


@extend_schema(
    summary="Get Tally processing funnel data",
    description="Returns funnel analysis of Tally bill and payment voucher processing stages",
    tags=["Tally Dashboard"],
    responses={200: TallyFunnelResponseSerializer, 404: ErrorResponseSerializer},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tally_funnel_view(request, org_id):
    return build_funnel_response(
        org_id,
        vendor_bill_model=TallyVendorBill,
        expense_bill_model=TallyExpenseBill,
        payment_bill_model=TallyPaymentBill,
    )


@extend_schema(
    summary="Get Tally usage statistics",
    description="Returns usage statistics and activity metrics for Tally",
    tags=["Tally Dashboard"],
    responses={200: TallyUsageResponseSerializer, 404: ErrorResponseSerializer},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tally_usage_view(request, org_id):
    return build_usage_response(
        org_id,
        vendor_bill_model=TallyVendorBill,
        expense_bill_model=TallyExpenseBill,
        payment_bill_model=TallyPaymentBill,
        updated_at_field="updated_at",
    )
