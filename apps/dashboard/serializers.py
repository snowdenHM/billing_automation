from rest_framework import serializers


# Basic response serializers for the three dashboard views
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


class ErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()


# Tally Dashboard Serializers (mirroring Zoho structure)
class TallyOverviewResponseSerializer(serializers.Serializer):
    vendor_bills = BillStatsSerializer()
    expense_bills = BillStatsSerializer()
    financial_summary = FinancialSummarySerializer()
    vendor_count = serializers.IntegerField()
    recent_activity = RecentActivitySerializer()


class TallyFunnelResponseSerializer(serializers.Serializer):
    vendor_bills_funnel = FunnelDataSerializer()
    expense_bills_funnel = FunnelDataSerializer()


class TallyUsageResponseSerializer(serializers.Serializer):
    usage_by_period = serializers.DictField(child=UsageStatsSerializer())
    file_statistics = FileStatsSerializer()
