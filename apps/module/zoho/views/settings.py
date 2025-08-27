import requests
from typing import Dict, Any, List, Tuple

from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.generics import RetrieveUpdateAPIView, ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema, OpenApiParameter

from apps.organizations.models import Organization
from apps.module.zoho.models import (
    ZohoCredentials,
    ZohoVendor,
    ZohoChartOfAccount,
    ZohoTaxes,
    ZohoTdsTcs,
    ZohoVendorCredit,
)
from apps.module.zoho.permissions import IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled
from apps.module.zoho.serializers import (
    EmptySerializer,
    SyncResultSerializer,
    GenerateTokenResponseSerializer,
    ZohoCredentialsSerializer,
    ZohoVendorSerializer,
    ZohoChartOfAccountSerializer,
    ZohoTaxesSerializer,
    ZohoTdsTcsSerializer,  # Fixed casing to match the serializer definition
    ZohoVendorCreditsSerializer,
)


# ------------------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------------------

class ZohoClient:
    BASE = "https://www.zohoapis.in/books/v3"
    TIMEOUT = 30

    def __init__(self, creds: ZohoCredentials):
        self.creds = creds
        self.headers = {"Authorization": f"Zoho-oauthtoken {self.creds.accessToken}"}

    def get(self, path: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
        params = dict(params or {})
        params["organization_id"] = self.creds.organisationId
        url = f"{self.BASE}/{path.lstrip('/')}"
        resp = requests.get(url, headers=self.headers, params=params, timeout=self.TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def exchange_code_for_tokens(creds: ZohoCredentials) -> Dict[str, Any]:
        url = "https://accounts.zoho.in/oauth/v2/token"
        payload = {
            "grant_type": "authorization_code",
            "code": creds.accessCode,
            "client_id": creds.clientId,
            "client_secret": creds.clientSecret,
            "redirect_uri": creds.redirectUrl,
        }
        resp = requests.post(url, data=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()


def _get_org_and_creds(org_id):
    """
    Helper to get organization and its Zoho credentials.
    Updated to handle UUID organization IDs.
    """
    org = get_object_or_404(Organization, id=org_id)
    creds = get_object_or_404(ZohoCredentials, organization=org)
    return org, creds


# ------------------------------------------------------------------------------
# Credentials
# ------------------------------------------------------------------------------

@extend_schema(tags=["Zoho"], responses=ZohoCredentialsSerializer)
class ZohoCredentialsView(RetrieveUpdateAPIView):
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = ZohoCredentialsSerializer
    lookup_url_kwarg = "org_id"
    queryset = ZohoCredentials.objects.none()  # for schema gen

    def get_object(self):
        org_id = self.kwargs.get("org_id")
        org = get_object_or_404(Organization, id=org_id)
        obj, _ = ZohoCredentials.objects.get_or_create(organization=org, defaults={})
        return obj


@extend_schema(
    tags=["Zoho"],
    request=EmptySerializer,
    responses=GenerateTokenResponseSerializer,
)
class GenerateTokenView(APIView):
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = GenerateTokenResponseSerializer  # helps spectacular

    def post(self, request, org_id, *args, **kwargs):
        _, creds = _get_org_and_creds(org_id)
        try:
            data = ZohoClient.exchange_code_for_tokens(creds)
        except requests.RequestException as e:
            return Response({"detail": f"Zoho auth failed: {e}"}, status=status.HTTP_502_BAD_GATEWAY)

        if "access_token" not in data:
            return Response({"detail": "access_token missing in Zoho response"}, status=status.HTTP_502_BAD_GATEWAY)

        creds.accessToken = data.get("access_token")
        if "refresh_token" in data and data["refresh_token"]:
            creds.refreshToken = data["refresh_token"]
        creds.save(update_fields=["accessToken", "refreshToken", "update_at"])

        out = {"accessToken": creds.accessToken, "refreshToken": creds.refreshToken or ""}
        return Response(GenerateTokenResponseSerializer(out).data, status=status.HTTP_200_OK)


# ------------------------------------------------------------------------------
# Vendors
# ------------------------------------------------------------------------------

@extend_schema(tags=["Zoho"], responses=ZohoVendorSerializer(many=True))
class VendorListView(ListAPIView):
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = ZohoVendorSerializer
    queryset = ZohoVendor.objects.none()  # prevent spectacular error

    def get_queryset(self):
        # Avoid accessing kwargs when spectacular generates schema
        if getattr(self, "swagger_fake_view", False):
            return ZohoVendor.objects.none()
        return ZohoVendor.objects.filter(organization_id=self.kwargs["org_id"]).order_by("-created_at")


@extend_schema(tags=["Zoho"], request=EmptySerializer, responses=SyncResultSerializer)
class VendorSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = SyncResultSerializer

    def post(self, request, org_id, *args, **kwargs):
        org, creds = _get_org_and_creds(org_id)
        client = ZohoClient(creds)
        try:
            payload = client.get("contacts")
        except requests.RequestException as e:
            return Response({"detail": f"Fetch vendors failed: {e}"}, status=502)

        contacts = payload.get("contacts", [])
        vendors = [c for c in contacts if c.get("contact_type") == "vendor"]

        existing_ids = set(
            ZohoVendor.objects.filter(organization=org, contactId__in=[v.get("contact_id") for v in vendors])
            .values_list("contactId", flat=True)
        )

        to_create: List[ZohoVendor] = []
        for v in vendors:
            if v.get("contact_id") not in existing_ids:
                to_create.append(
                    ZohoVendor(
                        organization=org,
                        contactId=v.get("contact_id"),
                        companyName=v.get("company_name") or "",
                        gstNo=v.get("gst_no") or "",
                    )
                )
        if to_create:
            ZohoVendor.objects.bulk_create(to_create, ignore_conflicts=True)

        return Response({"created": len(to_create), "total_vendors_seen": len(vendors)})


# ------------------------------------------------------------------------------
# Chart of Accounts
# ------------------------------------------------------------------------------

@extend_schema(tags=["Zoho"], responses=ZohoChartOfAccountSerializer(many=True))
class ChartOfAccountListView(ListAPIView):
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = ZohoChartOfAccountSerializer
    queryset = ZohoChartOfAccount.objects.none()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ZohoChartOfAccount.objects.none()
        return ZohoChartOfAccount.objects.filter(organization_id=self.kwargs["org_id"]).order_by("-created_at")


@extend_schema(tags=["Zoho"], request=EmptySerializer, responses=SyncResultSerializer)
class ChartOfAccountSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = SyncResultSerializer

    def post(self, request, org_id, *args, **kwargs):
        org, creds = _get_org_and_creds(org_id)
        client = ZohoClient(creds)
        try:
            payload = client.get("chartofaccounts")
        except requests.RequestException as e:
            return Response({"detail": f"Fetch chart of accounts failed: {e}"}, status=502)

        items = payload.get("chartofaccounts", [])
        existing_ids = set(
            ZohoChartOfAccount.objects.filter(organization=org, accountId__in=[a.get("account_id") for a in items])
            .values_list("accountId", flat=True)
        )

        to_create: List[ZohoChartOfAccount] = []
        for a in items:
            acc_id = a.get("account_id")
            if acc_id not in existing_ids:
                to_create.append(
                    ZohoChartOfAccount(
                        organization=org,
                        accountId=acc_id or "",
                        accountName=a.get("account_name") or "",
                    )
                )
        if to_create:
            ZohoChartOfAccount.objects.bulk_create(to_create, ignore_conflicts=True)

        return Response({"created": len(to_create), "total_accounts_seen": len(items)})


# ------------------------------------------------------------------------------
# Taxes (regular)
# ------------------------------------------------------------------------------

@extend_schema(tags=["Zoho"], responses=ZohoTaxesSerializer(many=True))
class TaxesListView(ListAPIView):
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = ZohoTaxesSerializer
    queryset = ZohoTaxes.objects.none()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ZohoTaxes.objects.none()
        return ZohoTaxes.objects.filter(organization_id=self.kwargs["org_id"]).order_by("-created_at")


@extend_schema(tags=["Zoho"], request=EmptySerializer, responses=SyncResultSerializer)
class TaxesSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = SyncResultSerializer

    def post(self, request, org_id, *args, **kwargs):
        org, creds = _get_org_and_creds(org_id)
        client = ZohoClient(creds)
        try:
            payload = client.get("settings/taxes")
        except requests.RequestException as e:
            return Response({"detail": f"Fetch taxes failed: {e}"}, status=502)

        taxes = payload.get("taxes", [])
        existing_ids = set(
            ZohoTaxes.objects.filter(organization=org, taxId__in=[t.get("tax_id") for t in taxes])
            .values_list("taxId", flat=True)
        )

        to_create: List[ZohoTaxes] = []
        for t in taxes:
            tax_id = t.get("tax_id")
            if tax_id not in existing_ids:
                to_create.append(
                    ZohoTaxes(
                        organization=org,
                        taxId=tax_id or "",
                        taxName=t.get("tax_name") or "",
                    )
                )
        if to_create:
            ZohoTaxes.objects.bulk_create(to_create, ignore_conflicts=True)

        return Response({"created": len(to_create), "total_taxes_seen": len(taxes)})


# ------------------------------------------------------------------------------
# TDS / TCS
# ------------------------------------------------------------------------------

@extend_schema(
    tags=["Zoho"],
    parameters=[OpenApiParameter(name="tax_type", required=False, type=str, location=OpenApiParameter.QUERY)],
    responses=ZohoTdsTcsSerializer(many=True),
)
class TDSTCSListView(ListAPIView):
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = ZohoTdsTcsSerializer
    queryset = ZohoTdsTcs.objects.none()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ZohoTdsTcs.objects.none()
        qs = ZohoTdsTcs.objects.filter(organization_id=self.kwargs["org_id"]).order_by("-created_at")
        ttype = self.request.query_params.get("tax_type")
        if ttype in {"TDS", "TCS"}:
            qs = qs.filter(taxType=ttype)
        return qs


@extend_schema(tags=["Zoho"], request=EmptySerializer, responses=SyncResultSerializer)
class TDSTCSSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = SyncResultSerializer

    def post(self, request, org_id, *args, **kwargs):
        org, creds = _get_org_and_creds(org_id)
        client = ZohoClient(creds)

        try:
            tds_payload = client.get("settings/taxes", params={"is_tds_request": "true"})
            tcs_payload = client.get("settings/taxes", params={"is_tcs_request": "true", "filter_by": "Taxes.All"})
        except requests.RequestException as e:
            return Response({"detail": f"Fetch TDS/TCS failed: {e}"}, status=502)

        def upsert(taxes: List[dict], ttype: str) -> int:
            ids = [x.get("tax_id") for x in taxes]
            existing = set(
                ZohoTdsTcs.objects.filter(organization=org, taxType=ttype, taxId__in=ids)
                .values_list("taxId", flat=True)
            )
            to_create: List[ZohoTdsTcs] = []
            for x in taxes:
                if x.get("tax_id") not in existing:
                    to_create.append(
                        ZohoTdsTcs(
                            organization=org,
                            taxId=x.get("tax_id") or "",
                            taxName=x.get("tax_name") or "",
                            taxPercentage=str(x.get("tax_percentage") or "0"),
                            taxType=ttype,
                        )
                    )
            if to_create:
                ZohoTdsTcs.objects.bulk_create(to_create, ignore_conflicts=True)
            return len(to_create)

        return Response({
            "created_tds": upsert(tds_payload.get("taxes", []), "TDS"),
            "created_tcs": upsert(tcs_payload.get("taxes", []), "TCS"),
        })
# ------------------------------------------------------------------------------
# Vendor Credits
# ------------------------------------------------------------------------------

@extend_schema(tags=["Zoho"], responses=ZohoVendorCreditsSerializer(many=True))
class VendorCreditsListView(ListAPIView):
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = ZohoVendorCreditsSerializer
    queryset = ZohoVendorCredit.objects.none()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ZohoVendorCredit.objects.none()
        return ZohoVendorCredit.objects.filter(organization_id=self.kwargs["org_id"]).order_by("-created_at")


@extend_schema(tags=["Zoho"], request=EmptySerializer, responses=SyncResultSerializer)
class VendorCreditsSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = SyncResultSerializer

    def post(self, request, org_id, *args, **kwargs):
        org, creds = _get_org_and_creds(org_id)
        client = ZohoClient(creds)
        try:
            payload = client.get("vendorcredits")
        except requests.RequestException as e:
            return Response({"detail": f"Fetch vendor credits failed: {e}"}, status=502)

        credits = payload.get("vendor_credits", [])
        existing_ids = set(
            ZohoVendorCredit.objects.filter(
                organization=org, vendor_credit_id__in=[vc.get("vendor_credit_id") for vc in credits]
            ).values_list("vendor_credit_id", flat=True)
        )

        to_create: List[ZohoVendorCredit] = []
        for vc in credits:
            vcid = vc.get("vendor_credit_id")
            if vcid not in existing_ids:
                to_create.append(
                    ZohoVendorCredit(
                        organization=org,
                        vendor_credit_id=vcid or "",
                        vendor_credit_number=vc.get("vendor_credit_number") or "",
                        vendor_id=vc.get("vendor_id") or "",
                        vendor_name=vc.get("vendor_name") or "",
                    )
                )
        if to_create:
            ZohoVendorCredit.objects.bulk_create(to_create, ignore_conflicts=True)

        return Response({"created": len(to_create)})


# ------------------------------------------------------------------------------
# Misc helper
# ------------------------------------------------------------------------------

@extend_schema(
    tags=["Zoho"],
    parameters=[OpenApiParameter(name="vendor_id", required=True, type=str, location=OpenApiParameter.QUERY)],
    responses={"200": dict},
)
class VendorGSTLookupView(APIView):
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = EmptySerializer

    def get(self, request, org_id, *args, **kwargs):
        vendor_pk = request.query_params.get("vendor_id")
        vendor = get_object_or_404(ZohoVendor, id=vendor_pk, organization_id=org_id)
        return Response({"gst": vendor.gstNo or "N/A"})
