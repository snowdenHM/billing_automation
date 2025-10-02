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


class ZohoOverviewView(APIView):
    """
    Provides overview statistics for Zoho module including total bills,
    processed amounts, vendor counts, and status distribution.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get Zoho overview statistics",
        description="Returns comprehensive overview of Zoho bills, vendors, and processing status",
        tags=["Zoho Dashboard"]
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


class ZohoTimeseriesView(APIView):
    """
    Provides time-series data for Zoho bills creation and processing trends.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get Zoho timeseries data",
        description="Returns time-series data for bill creation and processing trends",
        tags=["Zoho Dashboard"]
    )
    def get(self, request, org_id):
        try:
            organization = Organization.objects.get(id=org_id)
            period = request.query_params.get('period', 'day')  # day, week, month
            days = int(request.query_params.get('days', 30))

            start_date = timezone.now() - timedelta(days=days)

            # Choose truncation function based on period
            trunc_func = TruncDay if period == 'day' else TruncWeek if period == 'week' else TruncMonth

            # Vendor Bills Timeseries
            vendor_timeseries = (
                VendorBill.objects
                .filter(organization=organization, created_at__gte=start_date)
                .annotate(period=trunc_func('created_at'))
                .values('period')
                .annotate(
                    total_count=Count('id'),
                    draft_count=Count('id', filter=Q(status='Draft')),
                    analysed_count=Count('id', filter=Q(status__in=['Analysed', 'Verified'])),
                    synced_count=Count('id', filter=Q(status='Synced'))
                )
                .order_by('period')
            )

            # Expense Bills Timeseries
            expense_timeseries = (
                ExpenseBill.objects
                .filter(organization=organization, created_at__gte=start_date)
                .annotate(period=trunc_func('created_at'))
                .values('period')
                .annotate(
                    total_count=Count('id'),
                    draft_count=Count('id', filter=Q(status='Draft')),
                    analysed_count=Count('id', filter=Q(status__in=['Analysed', 'Verified'])),
                    synced_count=Count('id', filter=Q(status='Synced'))
                )
                .order_by('period')
            )

            return Response({
                'vendor_bills_timeseries': list(vendor_timeseries),
                'expense_bills_timeseries': list(expense_timeseries),
                'period': period,
                'days': days
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
        tags=["Zoho Dashboard"]
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
                    return {}
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


class ZohoTopVendorsView(APIView):
    """
    Provides top vendors analysis by bill count and total amounts.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get top Zoho vendors",
        description="Returns top vendors by bill count and amounts",
        tags=["Zoho Dashboard"]
    )
    def get(self, request, org_id):
        try:
            organization = Organization.objects.get(id=org_id)
            limit = int(request.query_params.get('limit', 10))

            # Top vendors by vendor bill count
            vendor_bill_stats = (
                VendorZohoBill.objects
                .filter(organization=organization, vendor__isnull=False)
                .values('vendor__companyName', 'vendor__contactId')
                .annotate(
                    bill_count=Count('id'),
                    total_amount=Sum('total')
                )
                .order_by('-bill_count')[:limit]
            )

            # Top vendors by expense bill count
            expense_bill_stats = (
                ExpenseZohoBill.objects
                .filter(organization=organization, vendor__isnull=False)
                .values('vendor__companyName', 'vendor__contactId')
                .annotate(
                    bill_count=Count('id'),
                    total_amount=Sum('total')
                )
                .order_by('-bill_count')[:limit]
            )

            return Response({
                'top_vendors_by_vendor_bills': list(vendor_bill_stats),
                'top_vendors_by_expense_bills': list(expense_bill_stats),
                'limit': limit
            })

        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class ZohoTaxesSummaryView(APIView):
    """
    Provides tax analysis including IGST, CGST, SGST summaries.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get Zoho taxes summary",
        description="Returns comprehensive tax analysis for bills",
        tags=["Zoho Dashboard"]
    )
    def get(self, request, org_id):
        try:
            organization = Organization.objects.get(id=org_id)

            # Vendor Bills Tax Summary
            vendor_bills = VendorZohoBill.objects.filter(organization=organization)
            vendor_tax_summary = {
                'total_igst': sum(float(bill.igst) for bill in vendor_bills if bill.igst),
                'total_cgst': sum(float(bill.cgst) for bill in vendor_bills if bill.cgst),
                'total_sgst': sum(float(bill.sgst) for bill in vendor_bills if bill.sgst),
                'bill_count': vendor_bills.count()
            }

            # Expense Bills Tax Summary
            expense_bills = ExpenseZohoBill.objects.filter(organization=organization)
            expense_tax_summary = {
                'total_igst': sum(float(bill.igst) for bill in expense_bills if bill.igst),
                'total_cgst': sum(float(bill.cgst) for bill in expense_bills if bill.cgst),
                'total_sgst': sum(float(bill.sgst) for bill in expense_bills if bill.sgst),
                'bill_count': expense_bills.count()
            }

            # Combined Tax Summary
            combined_summary = {
                'total_igst': vendor_tax_summary['total_igst'] + expense_tax_summary['total_igst'],
                'total_cgst': vendor_tax_summary['total_cgst'] + expense_tax_summary['total_cgst'],
                'total_sgst': vendor_tax_summary['total_sgst'] + expense_tax_summary['total_sgst'],
                'total_tax': (
                    vendor_tax_summary['total_igst'] + vendor_tax_summary['total_cgst'] + vendor_tax_summary['total_sgst'] +
                    expense_tax_summary['total_igst'] + expense_tax_summary['total_cgst'] + expense_tax_summary['total_sgst']
                )
            }

            return Response({
                'vendor_bills_taxes': vendor_tax_summary,
                'expense_bills_taxes': expense_tax_summary,
                'combined_taxes': combined_summary
            })

        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class ZohoExpenseSummaryView(APIView):
    """
    Provides expense analysis and categorization.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get Zoho expense summary",
        description="Returns expense analysis and categorization",
        tags=["Zoho Dashboard"]
    )
    def get(self, request, org_id):
        try:
            organization = Organization.objects.get(id=org_id)

            # Expense Bills by Status
            expense_bills = ExpenseBill.objects.filter(organization=organization)
            expense_summary = {
                'total_expense_bills': expense_bills.count(),
                'draft_expenses': expense_bills.filter(status='Draft').count(),
                'analysed_expenses': expense_bills.filter(status__in=['Analysed', 'Verified']).count(),
                'synced_expenses': expense_bills.filter(status='Synced').count(),
            }

            # Expense amounts by chart of accounts (top categories)
            expense_by_category = (
                ExpenseZohoProduct.objects
                .filter(organization=organization, chart_of_accounts__isnull=False)
                .values('chart_of_accounts__accountName')
                .annotate(
                    total_amount=Sum('amount'),
                    product_count=Count('id')
                )
                .order_by('-total_amount')[:10]
            )

            # Monthly expense trends
            monthly_expenses = (
                ExpenseBill.objects
                .filter(organization=organization)
                .annotate(month=TruncMonth('created_at'))
                .values('month')
                .annotate(
                    count=Count('id'),
                    total_amount=Sum('expensezohobill__total')
                )
                .order_by('month')
            )

            return Response({
                'expense_summary': expense_summary,
                'expense_by_category': list(expense_by_category),
                'monthly_trends': list(monthly_expenses)
            })

        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class ZohoPendingView(APIView):
    """
    Provides analysis of pending items requiring attention.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get Zoho pending items",
        description="Returns pending bills and items requiring attention",
        tags=["Zoho Dashboard"]
    )
    def get(self, request, org_id):
        try:
            organization = Organization.objects.get(id=org_id)

            # Pending Vendor Bills
            pending_vendor_bills = {
                'draft_bills': VendorBill.objects.filter(
                    organization=organization,
                    status='Draft'
                ).count(),
                'analysed_not_verified': VendorBill.objects.filter(
                    organization=organization,
                    status='Analysed'
                ).count(),
                'verified_not_synced': VendorBill.objects.filter(
                    organization=organization,
                    status='Verified'
                ).count(),
            }

            # Pending Expense Bills
            pending_expense_bills = {
                'draft_bills': ExpenseBill.objects.filter(
                    organization=organization,
                    status='Draft'
                ).count(),
                'analysed_not_verified': ExpenseBill.objects.filter(
                    organization=organization,
                    status='Analysed'
                ).count(),
                'verified_not_synced': ExpenseBill.objects.filter(
                    organization=organization,
                    status='Verified'
                ).count(),
            }

            # Oldest pending items
            oldest_vendor_draft = VendorBill.objects.filter(
                organization=organization,
                status='Draft'
            ).order_by('created_at').first()

            oldest_expense_draft = ExpenseBill.objects.filter(
                organization=organization,
                status='Draft'
            ).order_by('created_at').first()

            return Response({
                'pending_vendor_bills': pending_vendor_bills,
                'pending_expense_bills': pending_expense_bills,
                'oldest_pending': {
                    'vendor_bill': {
                        'id': str(oldest_vendor_draft.id) if oldest_vendor_draft else None,
                        'created_at': oldest_vendor_draft.created_at if oldest_vendor_draft else None,
                        'days_pending': (timezone.now() - oldest_vendor_draft.created_at).days if oldest_vendor_draft else 0
                    },
                    'expense_bill': {
                        'id': str(oldest_expense_draft.id) if oldest_expense_draft else None,
                        'created_at': oldest_expense_draft.created_at if oldest_expense_draft else None,
                        'days_pending': (timezone.now() - oldest_expense_draft.created_at).days if oldest_expense_draft else 0
                    }
                }
            })

        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class ZohoIntegrationsHealthView(APIView):
    """
    Provides health status of Zoho integrations and credentials.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get Zoho integrations health",
        description="Returns health status of Zoho credentials and integrations",
        tags=["Zoho Dashboard"]
    )
    def get(self, request, org_id):
        try:
            organization = Organization.objects.get(id=org_id)

            # Check Zoho Credentials
            try:
                credentials = ZohoCredentials.objects.get(organization=organization)
                credentials_status = {
                    'exists': True,
                    'client_id_set': bool(credentials.clientId),
                    'access_token_set': bool(credentials.accessToken),
                    'refresh_token_set': bool(credentials.refreshToken),
                    'organization_id_set': bool(credentials.organisationId),
                }
            except ZohoCredentials.DoesNotExist:
                credentials_status = {
                    'exists': False,
                    'client_id_set': False,
                    'access_token_set': False,
                    'refresh_token_set': False,
                    'organization_id_set': False,
                }

            # Integration Success Rates
            total_vendor_bills = VendorBill.objects.filter(organization=organization).count()
            synced_vendor_bills = VendorBill.objects.filter(
                organization=organization,
                status='Synced'
            ).count()

            total_expense_bills = ExpenseBill.objects.filter(organization=organization).count()
            synced_expense_bills = ExpenseBill.objects.filter(
                organization=organization,
                status='Synced'
            ).count()

            success_rates = {
                'vendor_bills_sync_rate': (synced_vendor_bills / total_vendor_bills * 100) if total_vendor_bills > 0 else 0,
                'expense_bills_sync_rate': (synced_expense_bills / total_expense_bills * 100) if total_expense_bills > 0 else 0,
                'overall_sync_rate': (
                    (synced_vendor_bills + synced_expense_bills) /
                    (total_vendor_bills + total_expense_bills) * 100
                ) if (total_vendor_bills + total_expense_bills) > 0 else 0
            }

            return Response({
                'credentials_status': credentials_status,
                'success_rates': success_rates,
                'health_score': min(100, (
                    (credentials_status['exists'] * 40) +
                    (success_rates['overall_sync_rate'] * 0.6)
                ))
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
        tags=["Zoho Dashboard"]
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
