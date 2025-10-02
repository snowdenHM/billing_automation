from rest_framework import serializers


class OverviewSerializer(serializers.Serializer):
    totals = serializers.DictField(child=serializers.IntegerField())
    sync_rate = serializers.FloatField()
    avg_analyse_time_sec = serializers.FloatField(allow_null=True)
    avg_verify_time_sec = serializers.FloatField(allow_null=True)


class TimeseriesPointSerializer(serializers.Serializer):
    date = serializers.DateField()
    uploaded = serializers.IntegerField()
    analysed = serializers.IntegerField()
    verified = serializers.IntegerField()
    synced = serializers.IntegerField()


class TimeseriesResponseSerializer(serializers.Serializer):
    series = TimeseriesPointSerializer(many=True)


class FunnelStepSerializer(serializers.Serializer):
    stage = serializers.CharField()
    count = serializers.IntegerField()


class FunnelResponseSerializer(serializers.Serializer):
    funnel = FunnelStepSerializer(many=True)
    dropoffs = serializers.DictField(child=serializers.IntegerField())


class TopVendorItemSerializer(serializers.Serializer):
    name = serializers.CharField()
    count = serializers.IntegerField()
    total_amount = serializers.DecimalField(max_digits=14, decimal_places=2)


class TopVendorsResponseSerializer(serializers.Serializer):
    vendors = TopVendorItemSerializer(many=True)


class TaxesSummarySerializer(serializers.Serializer):
    gst_mix = serializers.DictField(child=serializers.DecimalField(max_digits=14, decimal_places=2))


class ExpenseByAccountSerializer(serializers.Serializer):
    account_name = serializers.CharField()
    debit = serializers.DecimalField(max_digits=14, decimal_places=2)
    credit = serializers.DecimalField(max_digits=14, decimal_places=2)


# Zoho Dashboard Serializers
class BillStatsSerializer(serializers.Serializer):
    total_count = serializers.IntegerField()
    draft_count = serializers.IntegerField()
    analysed_count = serializers.IntegerField()
    synced_count = serializers.IntegerField()


class FinancialSummarySerializer(serializers.Serializer):
    total_vendor_amount = serializers.FloatField()
    total_expense_amount = serializers.FloatField()
    combined_amount = serializers.FloatField()


class RecentActivitySerializer(serializers.Serializer):
    vendor_bills_last_7_days = serializers.IntegerField()
    expense_bills_last_7_days = serializers.IntegerField()


class ZohoOverviewResponseSerializer(serializers.Serializer):
    vendor_bills = BillStatsSerializer()
    expense_bills = BillStatsSerializer()
    financial_summary = FinancialSummarySerializer()
    vendor_count = serializers.IntegerField()
    recent_activity = RecentActivitySerializer()


class TimeseriesDataSerializer(serializers.Serializer):
    period = serializers.DateTimeField()
    total_count = serializers.IntegerField()
    draft_count = serializers.IntegerField()
    analysed_count = serializers.IntegerField()
    synced_count = serializers.IntegerField()


class ZohoTimeseriesResponseSerializer(serializers.Serializer):
    vendor_bills_timeseries = TimeseriesDataSerializer(many=True)
    expense_bills_timeseries = TimeseriesDataSerializer(many=True)
    period = serializers.CharField()
    days = serializers.IntegerField()


class ConversionRatesSerializer(serializers.Serializer):
    analysis_rate = serializers.FloatField()
    verification_rate = serializers.FloatField()
    sync_rate = serializers.FloatField()


class FunnelDataSerializer(serializers.Serializer):
    total_uploaded = serializers.IntegerField()
    draft = serializers.IntegerField()
    analysed = serializers.IntegerField()
    verified = serializers.IntegerField()
    synced = serializers.IntegerField()
    conversion_rates = ConversionRatesSerializer()


class ZohoFunnelResponseSerializer(serializers.Serializer):
    vendor_bills_funnel = FunnelDataSerializer()
    expense_bills_funnel = FunnelDataSerializer()


class VendorStatsSerializer(serializers.Serializer):
    vendor__companyName = serializers.CharField()
    vendor__contactId = serializers.CharField()
    bill_count = serializers.IntegerField()
    total_amount = serializers.DecimalField(max_digits=15, decimal_places=2, allow_null=True)


class ZohoTopVendorsResponseSerializer(serializers.Serializer):
    top_vendors_by_vendor_bills = VendorStatsSerializer(many=True)
    top_vendors_by_expense_bills = VendorStatsSerializer(many=True)
    limit = serializers.IntegerField()


class TaxSummarySerializer(serializers.Serializer):
    total_igst = serializers.FloatField()
    total_cgst = serializers.FloatField()
    total_sgst = serializers.FloatField()
    bill_count = serializers.IntegerField()


class CombinedTaxSummarySerializer(serializers.Serializer):
    total_igst = serializers.FloatField()
    total_cgst = serializers.FloatField()
    total_sgst = serializers.FloatField()
    total_tax = serializers.FloatField()


class ZohoTaxesResponseSerializer(serializers.Serializer):
    vendor_bills_taxes = TaxSummarySerializer()
    expense_bills_taxes = TaxSummarySerializer()
    combined_taxes = CombinedTaxSummarySerializer()


class ExpenseCategorySerializer(serializers.Serializer):
    chart_of_accounts__accountName = serializers.CharField()
    total_amount = serializers.DecimalField(max_digits=15, decimal_places=2, allow_null=True)
    product_count = serializers.IntegerField()


class MonthlyExpenseSerializer(serializers.Serializer):
    month = serializers.DateTimeField()
    count = serializers.IntegerField()
    total_amount = serializers.DecimalField(max_digits=15, decimal_places=2, allow_null=True)


class ZohoExpenseResponseSerializer(serializers.Serializer):
    expense_summary = BillStatsSerializer()
    expense_by_category = ExpenseCategorySerializer(many=True)
    monthly_trends = MonthlyExpenseSerializer(many=True)


class ErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()


class UsageStatsSerializer(serializers.Serializer):
    vendor_bills_uploaded = serializers.IntegerField()
    expense_bills_uploaded = serializers.IntegerField()
    bills_analysed = serializers.IntegerField()
    bills_synced = serializers.IntegerField()


class FileStatsSerializer(serializers.Serializer):
    total_vendor_files = serializers.IntegerField()
    total_expense_files = serializers.IntegerField()
    total_files = serializers.IntegerField()


class ZohoUsageResponseSerializer(serializers.Serializer):
    usage_by_period = serializers.DictField(child=UsageStatsSerializer())
    file_statistics = FileStatsSerializer()


class CredentialsStatusSerializer(serializers.Serializer):
    exists = serializers.BooleanField()
    client_id_set = serializers.BooleanField()
    access_token_set = serializers.BooleanField()
    refresh_token_set = serializers.BooleanField()
    organization_id_set = serializers.BooleanField()


class SuccessRatesSerializer(serializers.Serializer):
    vendor_bills_sync_rate = serializers.FloatField()
    expense_bills_sync_rate = serializers.FloatField()
    overall_sync_rate = serializers.FloatField()


class ZohoHealthResponseSerializer(serializers.Serializer):
    credentials_status = CredentialsStatusSerializer()
    success_rates = SuccessRatesSerializer()
    health_score = serializers.FloatField()
