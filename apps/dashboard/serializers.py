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


class ExpenseSummarySerializer(serializers.Serializer):
    totals = serializers.DictField(child=serializers.DecimalField(max_digits=14, decimal_places=2))
    by_account = ExpenseByAccountSerializer(many=True)


class PendingItemSerializer(serializers.Serializer):
    type = serializers.CharField()
    id = serializers.CharField()
    name = serializers.CharField(allow_blank=True)
    status = serializers.CharField()
    age_hours = serializers.FloatField()


class PendingResponseSerializer(serializers.Serializer):
    items = PendingItemSerializer(many=True)
    counts = serializers.DictField(child=serializers.IntegerField())


class IntegrationsHealthSerializer(serializers.Serializer):
    ai_ocr = serializers.DictField()
    zoho = serializers.DictField()
    tally_tcp = serializers.DictField()


class UsageSerializer(serializers.Serializer):
    plan = serializers.DictField()
    usage = serializers.DictField()
    percent = serializers.DictField(child=serializers.FloatField())
