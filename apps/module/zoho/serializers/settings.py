from rest_framework import serializers
from apps.module.zoho.serializers.base import OrgField
from apps.module.zoho.models import (
    ZohoCredentials,
    ZohoVendor,
    ZohoChartOfAccount,
    ZohoTaxes,
    Zoho_TDS_TCS,
    Zoho_Vendor_Credits,
)


class ZohoCredentialsSerializer(serializers.ModelSerializer):
    organization = OrgField(required=True)

    class Meta:
        model = ZohoCredentials
        fields = [
            "id",
            "organization",
            "clientId",
            "clientSecret",
            "accessCode",
            "organisationId",
            "redirectUrl",
            "accessToken",
            "refreshToken",
            "created_at",
            "update_at",
        ]
        read_only_fields = ["id", "created_at", "update_at"]


class ZohoVendorSerializer(serializers.ModelSerializer):
    organization = OrgField(required=True)

    class Meta:
        model = ZohoVendor
        fields = ["id", "organization", "contactId", "companyName", "gstNo", "created_at"]
        read_only_fields = ["id", "created_at"]


class ZohoChartOfAccountSerializer(serializers.ModelSerializer):
    organization = OrgField(required=True)

    class Meta:
        model = ZohoChartOfAccount
        fields = ["id", "organization", "accountId", "accountName", "created_at"]
        read_only_fields = ["id", "created_at"]


class ZohoTaxesSerializer(serializers.ModelSerializer):
    organization = OrgField(required=True)

    class Meta:
        model = ZohoTaxes
        fields = ["id", "organization", "taxId", "taxName", "created_at"]
        read_only_fields = ["id", "created_at"]


class ZohoTDSTCSSerializer(serializers.ModelSerializer):
    organization = OrgField(required=True)

    class Meta:
        model = Zoho_TDS_TCS
        fields = [
            "id",
            "organization",
            "taxId",
            "taxName",
            "taxPercentage",
            "taxType",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class ZohoVendorCreditsSerializer(serializers.ModelSerializer):
    organization = OrgField(required=True)

    class Meta:
        model = Zoho_Vendor_Credits
        fields = [
            "id",
            "organization",
            "vendor_id",
            "vendor_name",
            "vendor_credit_id",
            "vendor_credit_number",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]
