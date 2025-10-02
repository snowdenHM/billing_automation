from django.db.models import Count, Sum, Q
from django.db.models.functions import TruncDay, TruncWeek, TruncMonth
from django.utils import timezone
from datetime import timedelta
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from drf_spectacular.utils import extend_schema

from apps.organizations.models import Organization
from apps.module.zoho.models import (
    VendorBill, VendorZohoBill,
    ExpenseBill, ExpenseZohoBill, ExpenseZohoProduct,
    ZohoVendor, ZohoCredentials
)
from apps.module.tally.models import (
    TallyVendorBill, TallyExpenseBill, TallyVendorAnalyzedBill,
    TallyExpenseAnalyzedBill, TallyExpenseAnalyzedProduct, Ledger
)
from .serializers import (
    ZohoOverviewResponseSerializer, ZohoFunnelResponseSerializer,
    ZohoUsageResponseSerializer, TallyOverviewResponseSerializer,
    TallyFunnelResponseSerializer, TallyUsageResponseSerializer,
    ErrorResponseSerializer
)


class ZohoOverviewView(APIView):
    """
    Provides overview statistics for Zoho module including total bills,
    processed amounts, vendor counts, and status distribution.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get Zoho overview statistics",
        description="Returns comprehensive overview of Zoho bills, vendors, and processing status",
        tags=["Zoho Dashboard"],
        responses={
            200: ZohoOverviewResponseSerializer,
            404: ErrorResponseSerializer
        }
    )
    def get(self, request, org_id):
        try:
            organization = Organization.objects.get(id=org_id)

            # Vendor Bills Statistics
            vendor_bills = VendorBill.objects.filter(organization=organization)
            vendor_bills_stats = {
                'total_count': vendor_bills.count(),
                'draft_count': vendor_bills.filter(status='Draft').count(),
                'analysed_count': vendor_bills.filter(status__in=['Analysed', 'Verified']).count(),
                'synced_count': vendor_bills.filter(status='Synced').count(),
            }

            # Expense Bills Statistics
            expense_bills = ExpenseBill.objects.filter(organization=organization)
            expense_bills_stats = {
                'total_count': expense_bills.count(),
                'draft_count': expense_bills.filter(status='Draft').count(),
                'analysed_count': expense_bills.filter(status__in=['Analysed', 'Verified']).count(),
                'synced_count': expense_bills.filter(status='Synced').count(),
            }

            # Financial Summary
            vendor_zoho_bills = VendorZohoBill.objects.filter(organization=organization)
            expense_zoho_bills = ExpenseZohoBill.objects.filter(organization=organization)

            total_vendor_amount = sum(float(bill.total) for bill in vendor_zoho_bills if bill.total)
            total_expense_amount = sum(float(bill.total) for bill in expense_zoho_bills if bill.total)

            # Vendor Statistics
            vendor_count = ZohoVendor.objects.filter(organization=organization).count()

            # Recent Activity (last 7 days)
            week_ago = timezone.now() - timedelta(days=7)
            recent_vendor_bills = vendor_bills.filter(created_at__gte=week_ago).count()
            recent_expense_bills = expense_bills.filter(created_at__gte=week_ago).count()

            return Response({
                'vendor_bills': vendor_bills_stats,
                'expense_bills': expense_bills_stats,
                'financial_summary': {
                    'total_vendor_amount': total_vendor_amount,
                    'total_expense_amount': total_expense_amount,
                    'combined_amount': total_vendor_amount + total_expense_amount
                },
                'vendor_count': vendor_count,
                'recent_activity': {
                    'vendor_bills_last_7_days': recent_vendor_bills,
                    'expense_bills_last_7_days': recent_expense_bills
                }
            })

        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class ZohoFunnelView(APIView):
    """
    Provides funnel analysis showing bill processing pipeline from Draft to Synced.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get Zoho processing funnel data",
        description="Returns funnel analysis of bill processing stages",
        tags=["Zoho Dashboard"],
        responses={
            200: ZohoFunnelResponseSerializer,
            404: ErrorResponseSerializer
        }
    )
    def get(self, request, org_id):
        try:
            organization = Organization.objects.get(id=org_id)

            # Vendor Bills Funnel
            vendor_bills = VendorBill.objects.filter(organization=organization)
            vendor_funnel = {
                'total_uploaded': vendor_bills.count(),
                'draft': vendor_bills.filter(status='Draft').count(),
                'analysed': vendor_bills.filter(status='Analysed').count(),
                'verified': vendor_bills.filter(status='Verified').count(),
                'synced': vendor_bills.filter(status='Synced').count(),
            }

            # Expense Bills Funnel
            expense_bills = ExpenseBill.objects.filter(organization=organization)
            expense_funnel = {
                'total_uploaded': expense_bills.count(),
                'draft': expense_bills.filter(status='Draft').count(),
                'analysed': expense_bills.filter(status='Analysed').count(),
                'verified': expense_bills.filter(status='Verified').count(),
                'synced': expense_bills.filter(status='Synced').count(),
            }

            # Calculate conversion rates
            def calculate_rates(funnel_data):
                total = funnel_data['total_uploaded']
                if total == 0:
                    return {
                        'analysis_rate': 0.0,
                        'verification_rate': 0.0,
                        'sync_rate': 0.0,
                    }
                return {
                    'analysis_rate': (funnel_data['analysed'] + funnel_data['verified'] + funnel_data['synced']) / total * 100,
                    'verification_rate': (funnel_data['verified'] + funnel_data['synced']) / total * 100,
                    'sync_rate': funnel_data['synced'] / total * 100,
                }

            return Response({
                'vendor_bills_funnel': {
                    **vendor_funnel,
                    'conversion_rates': calculate_rates(vendor_funnel)
                },
                'expense_bills_funnel': {
                    **expense_funnel,
                    'conversion_rates': calculate_rates(expense_funnel)
                }
            })

        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class ZohoUsageView(APIView):
    """
    Provides usage statistics and activity metrics.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get Zoho usage statistics",
        description="Returns usage statistics and activity metrics",
        tags=["Zoho Dashboard"],
        responses={
            200: ZohoUsageResponseSerializer,
            404: ErrorResponseSerializer
        }
    )
    def get(self, request, org_id):
        try:
            organization = Organization.objects.get(id=org_id)

            # Usage over time periods
            now = timezone.now()

            usage_stats = {}
            for period_name, days in [('today', 1), ('week', 7), ('month', 30), ('quarter', 90)]:
                start_date = now - timedelta(days=days)

                usage_stats[period_name] = {
                    'vendor_bills_uploaded': VendorBill.objects.filter(
                        organization=organization,
                        created_at__gte=start_date
                    ).count(),
                    'expense_bills_uploaded': ExpenseBill.objects.filter(
                        organization=organization,
                        created_at__gte=start_date
                    ).count(),
                    'bills_analysed': (
                        VendorBill.objects.filter(
                            organization=organization,
                            status__in=['Analysed', 'Verified', 'Synced'],
                            update_at__gte=start_date
                        ).count() +
                        ExpenseBill.objects.filter(
                            organization=organization,
                            status__in=['Analysed', 'Verified', 'Synced'],
                            update_at__gte=start_date
                        ).count()
                    ),
                    'bills_synced': (
                        VendorBill.objects.filter(
                            organization=organization,
                            status='Synced',
                            update_at__gte=start_date
                        ).count() +
                        ExpenseBill.objects.filter(
                            organization=organization,
                            status='Synced',
                            update_at__gte=start_date
                        ).count()
                    )
                }

            # Storage usage (file sizes)
            vendor_bills_with_files = VendorBill.objects.filter(
                organization=organization,
                file__isnull=False
            )
            expense_bills_with_files = ExpenseBill.objects.filter(
                organization=organization,
                file__isnull=False
            )

            return Response({
                'usage_by_period': usage_stats,
                'file_statistics': {
                    'total_vendor_files': vendor_bills_with_files.count(),
                    'total_expense_files': expense_bills_with_files.count(),
                    'total_files': vendor_bills_with_files.count() + expense_bills_with_files.count()
                }
            })

        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class TallyOverviewView(APIView):
    """
    Provides overview statistics for Tally module including total bills,
    processed amounts, vendor counts, and status distribution.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get Tally overview statistics",
        description="Returns comprehensive overview of Tally bills, vendors, and processing status",
        tags=["Tally Dashboard"],
        responses={
            200: TallyOverviewResponseSerializer,
            404: ErrorResponseSerializer
        }
    )
    def get(self, request, org_id):
        try:
            organization = Organization.objects.get(id=org_id)

            # Tally Vendor Bills Statistics
            vendor_bills = TallyVendorBill.objects.filter(organization=organization)
            vendor_bills_stats = {
                'total_count': vendor_bills.count(),
                'draft_count': vendor_bills.filter(status='Draft').count(),
                'analysed_count': vendor_bills.filter(status__in=['Analysed', 'Verified']).count(),
                'synced_count': vendor_bills.filter(status='Synced').count(),
            }

            # Tally Expense Bills Statistics
            expense_bills = TallyExpenseBill.objects.filter(organization=organization)
            expense_bills_stats = {
                'total_count': expense_bills.count(),
                'draft_count': expense_bills.filter(status='Draft').count(),
                'analysed_count': expense_bills.filter(status__in=['Analysed', 'Verified']).count(),
                'synced_count': expense_bills.filter(status='Synced').count(),
            }

            # Financial Summary from Tally Analyzed Bills
            vendor_analyzed_bills = TallyVendorAnalyzedBill.objects.filter(organization=organization)
            expense_analyzed_bills = TallyExpenseAnalyzedBill.objects.filter(organization=organization)

            total_vendor_amount = sum(float(bill.total) if bill.total else 0 for bill in vendor_analyzed_bills)
            total_expense_amount = sum(float(bill.total) if bill.total else 0 for bill in expense_analyzed_bills)

            # Ledger Statistics (equivalent to vendor count)
            ledger_count = Ledger.objects.filter(organization=organization).count()

            # Recent Activity (last 7 days)
            week_ago = timezone.now() - timedelta(days=7)
            recent_vendor_bills = vendor_bills.filter(created_at__gte=week_ago).count()
            recent_expense_bills = expense_bills.filter(created_at__gte=week_ago).count()

            return Response({
                'vendor_bills': vendor_bills_stats,
                'expense_bills': expense_bills_stats,
                'financial_summary': {
                    'total_vendor_amount': total_vendor_amount,
                    'total_expense_amount': total_expense_amount,
                    'combined_amount': total_vendor_amount + total_expense_amount
                },
                'vendor_count': ledger_count,
                'recent_activity': {
                    'vendor_bills_last_7_days': recent_vendor_bills,
                    'expense_bills_last_7_days': recent_expense_bills
                }
            })

        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class TallyFunnelView(APIView):
    """
    Provides funnel analysis showing Tally bill processing pipeline from Draft to Synced.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get Tally processing funnel data",
        description="Returns funnel analysis of Tally bill processing stages",
        tags=["Tally Dashboard"],
        responses={
            200: TallyFunnelResponseSerializer,
            404: ErrorResponseSerializer
        }
    )
    def get(self, request, org_id):
        try:
            organization = Organization.objects.get(id=org_id)

            # Tally Vendor Bills Funnel
            vendor_bills = TallyVendorBill.objects.filter(organization=organization)
            vendor_funnel = {
                'total_uploaded': vendor_bills.count(),
                'draft': vendor_bills.filter(status='Draft').count(),
                'analysed': vendor_bills.filter(status='Analysed').count(),
                'verified': vendor_bills.filter(status='Verified').count(),
                'synced': vendor_bills.filter(status='Synced').count(),
            }

            # Tally Expense Bills Funnel
            expense_bills = TallyExpenseBill.objects.filter(organization=organization)
            expense_funnel = {
                'total_uploaded': expense_bills.count(),
                'draft': expense_bills.filter(status='Draft').count(),
                'analysed': expense_bills.filter(status='Analysed').count(),
                'verified': expense_bills.filter(status='Verified').count(),
                'synced': expense_bills.filter(status='Synced').count(),
            }

            # Calculate conversion rates
            def calculate_rates(funnel_data):
                total = funnel_data['total_uploaded']
                if total == 0:
                    return {
                        'analysis_rate': 0.0,
                        'verification_rate': 0.0,
                        'sync_rate': 0.0,
                    }
                return {
                    'analysis_rate': (funnel_data['analysed'] + funnel_data['verified'] + funnel_data['synced']) / total * 100,
                    'verification_rate': (funnel_data['verified'] + funnel_data['synced']) / total * 100,
                    'sync_rate': funnel_data['synced'] / total * 100,
                }

            return Response({
                'vendor_bills_funnel': {
                    **vendor_funnel,
                    'conversion_rates': calculate_rates(vendor_funnel)
                },
                'expense_bills_funnel': {
                    **expense_funnel,
                    'conversion_rates': calculate_rates(expense_funnel)
                }
            })

        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class TallyUsageView(APIView):
    """
    Provides usage statistics and activity metrics for Tally.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get Tally usage statistics",
        description="Returns usage statistics and activity metrics for Tally",
        tags=["Tally Dashboard"],
        responses={
            200: TallyUsageResponseSerializer,
            404: ErrorResponseSerializer
        }
    )
    def get(self, request, org_id):
        try:
            organization = Organization.objects.get(id=org_id)

            # Usage over time periods
            now = timezone.now()

            usage_stats = {}
            for period_name, days in [('today', 1), ('week', 7), ('month', 30), ('quarter', 90)]:
                start_date = now - timedelta(days=days)

                usage_stats[period_name] = {
                    'vendor_bills_uploaded': TallyVendorBill.objects.filter(
                        organization=organization,
                        created_at__gte=start_date
                    ).count(),
                    'expense_bills_uploaded': TallyExpenseBill.objects.filter(
                        organization=organization,
                        created_at__gte=start_date
                    ).count(),
                    'bills_analysed': (
                        TallyVendorBill.objects.filter(
                            organization=organization,
                            status__in=['Analysed', 'Verified', 'Synced'],
                            update_at__gte=start_date
                        ).count() +
                        TallyExpenseBill.objects.filter(
                            organization=organization,
                            status__in=['Analysed', 'Verified', 'Synced'],
                            update_at__gte=start_date
                        ).count()
                    ),
                    'bills_synced': (
                        TallyVendorBill.objects.filter(
                            organization=organization,
                            status='Synced',
                            update_at__gte=start_date
                        ).count() +
                        TallyExpenseBill.objects.filter(
                            organization=organization,
                            status='Synced',
                            update_at__gte=start_date
                        ).count()
                    )
                }

            # Storage usage (file sizes)
            vendor_bills_with_files = TallyVendorBill.objects.filter(
                organization=organization,
                file__isnull=False
            )
            expense_bills_with_files = TallyExpenseBill.objects.filter(
                organization=organization,
                file__isnull=False
            )

            return Response({
                'usage_by_period': usage_stats,
                'file_statistics': {
                    'total_vendor_files': vendor_bills_with_files.count(),
                    'total_expense_files': expense_bills_with_files.count(),
                    'total_files': vendor_bills_with_files.count() + expense_bills_with_files.count()
                }
            })

        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )
