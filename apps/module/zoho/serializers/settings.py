from rest_framework import serializers
from apps.module.zoho.serializers.base import OrgField
from apps.module.zoho.models import (
    ZohoCredentials,
    ZohoVendor,
    ZohoCustomer,
    ZohoChartOfAccount,
    ZohoTaxes,
    ZohoTdsTcs,
    ZohoVendorCredit,
)


def _mask(value, keep=4):
    """``1000.abcd...WXYZ`` — enough to tell two credentials apart.

    Never the whole value. The point of showing anything at all is so an
    operator can confirm *which* credential is stored, not read it back
    out.
    """
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "•" * len(text)
    return f"{text[:keep]}{'•' * 8}{text[-keep:]}"


class ZohoCredentialsSerializer(serializers.ModelSerializer):
    """Zoho OAuth credentials, with the secrets write-only.

    The client secret, access token and refresh token used to be sent to
    the browser in full, and the settings page rendered them with a
    reveal button and a Copy button beside each one.

    A Zoho **refresh token does not expire** and grants full read/write
    access to the client's accounting books. Putting it in the DOM means
    any script on the page can read it, it rides along in devtools, HAR
    exports and screenshots, and it outlives any session we control. The
    same reasoning moved our own refresh token out of localStorage; this
    is the same credential class, belonging to someone else's books.

    So they are accepted on write and never returned on read. What comes
    back instead is a mask (enough to tell one credential from another)
    and a boolean saying whether each is set, which is all the settings
    page actually needs to render.
    """

    organization = OrgField(required=True)

    # The OAuth app identity belongs to the deployment, not to a
    # workspace. ``get_zoho_credentials`` rewrites all three from the
    # environment on every call, so a value saved here survived until
    # the next request and then silently reverted — a settings field
    # that takes input and discards it is worse than one that is
    # plainly disabled. The redirect URL in particular *must* match the
    # one registered on the Zoho app, so it cannot be per-workspace.
    clientId = serializers.CharField(read_only=True)
    redirectUrl = serializers.CharField(read_only=True)

    # Write-only: an admin can paste new values, nobody can read them back.
    accessCode = serializers.CharField(write_only=True, required=False, allow_blank=True)
    accessToken = serializers.CharField(write_only=True, required=False, allow_blank=True)
    refreshToken = serializers.CharField(write_only=True, required=False, allow_blank=True)

    clientSecret_masked = serializers.SerializerMethodField()
    accessToken_masked = serializers.SerializerMethodField()
    refreshToken_masked = serializers.SerializerMethodField()
    has_client_secret = serializers.SerializerMethodField()
    has_access_token = serializers.SerializerMethodField()
    has_refresh_token = serializers.SerializerMethodField()

    class Meta:
        model = ZohoCredentials
        fields = [
            "id",
            "organization",
            # clientId is a public OAuth identifier, not a secret.
            "clientId",
            "organisationId",
            "redirectUrl",
            # Which Zoho data centre this account lives in. An admin has
            # to be able to change it: the whole OAuth flow targets it,
            # and connecting an account on the wrong one fails in a way
            # that looks like a missing organisation.
            "region",
            # Write-only. The client secret is absent on purpose: it
            # comes from the environment, so accepting one here would
            # only discard it. Its mask and presence flag remain below.
            "accessCode",
            "accessToken",
            "refreshToken",
            # Read-only stand-ins for the four above
            "clientSecret_masked",
            "accessToken_masked",
            "refreshToken_masked",
            "has_client_secret",
            "has_access_token",
            "has_refresh_token",
            "token_expiry",
            "is_connected",
            "created_at",
            "update_at",
        ]
        read_only_fields = [
            "id", "created_at", "update_at", "token_expiry", "is_connected",
            "clientId", "redirectUrl",
        ]

    def get_clientSecret_masked(self, obj):
        return _mask(getattr(obj, "clientSecret", None))

    def get_accessToken_masked(self, obj):
        return _mask(getattr(obj, "accessToken", None))

    def get_refreshToken_masked(self, obj):
        return _mask(getattr(obj, "refreshToken", None))

    def get_has_client_secret(self, obj):
        return bool(getattr(obj, "clientSecret", None))

    def get_has_access_token(self, obj):
        return bool(getattr(obj, "accessToken", None))

    def get_has_refresh_token(self, obj):
        return bool(getattr(obj, "refreshToken", None))


class ZohoVendorSerializer(serializers.ModelSerializer):
    organization = OrgField(required=True)

    class Meta:
        model = ZohoVendor
        fields = ["id", "organization", "contactId", "companyName", "gstNo", "gst_treatment", "created_at"]
        read_only_fields = ["id", "created_at"]


class ZohoCustomerSerializer(serializers.ModelSerializer):
    organization = OrgField(required=True)

    class Meta:
        model = ZohoCustomer
        fields = [
            "id", "organization", "contactId", "companyName", "gstNo",
            "gst_treatment", "place_of_supply", "created_at",
        ]
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


class ZohoTdsTcsSerializer(serializers.ModelSerializer):
    organization = OrgField(required=True)

    class Meta:
        model = ZohoTdsTcs
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
        model = ZohoVendorCredit
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
