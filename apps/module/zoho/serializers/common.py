# apps/module/zoho/serializers/common.py

from rest_framework import serializers

# Common serializers for API responses

class TokenResponseSerializer(serializers.Serializer):
    """Serializer for token generation responses."""
    detail = serializers.CharField(required=False)
    access_token = serializers.CharField(required=False)
    refresh_token = serializers.CharField(required=False)
    expires_in = serializers.IntegerField(required=False)
    token_expiry = serializers.DateTimeField(required=False)
    error_code = serializers.CharField(required=False)


class SyncResponseSerializer(serializers.Serializer):
    """Serializer for sync operation responses."""
    detail = serializers.CharField()
    synced_count = serializers.IntegerField(required=False)


class AnalysisResponseSerializer(serializers.Serializer):
    """Serializer for bill analysis responses."""
    detail = serializers.CharField()
    analyzed_data = serializers.JSONField(required=False)


class ZohoSyncResponseSerializer(serializers.Serializer):
    """Serializer for Zoho sync operation responses."""
    detail = serializers.CharField()
    zoho_bill_id = serializers.CharField(required=False)
