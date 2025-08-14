from rest_framework import serializers
from apps.organizations.models import Organization


class OrgField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return Organization.objects.all()


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
