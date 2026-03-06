from rest_framework import serializers

from apps.common.serializers import OrgField  # noqa: F401


class EmptySerializer(serializers.Serializer):
    """Use for endpoints that don't take a body."""
    pass


class SyncResultSerializer(serializers.Serializer):
    """Generic sync response serializer."""
    created = serializers.IntegerField(required=False)
    total_vendors_seen = serializers.IntegerField(required=False)
    total_accounts_seen = serializers.IntegerField(required=False)
    total_taxes_seen = serializers.IntegerField(required=False)
    created_tds = serializers.IntegerField(required=False)
    created_tcs = serializers.IntegerField(required=False)


class GenerateTokenResponseSerializer(serializers.Serializer):
    accessToken = serializers.CharField()
    refreshToken = serializers.CharField(allow_blank=True, required=False)
