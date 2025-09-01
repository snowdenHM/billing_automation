from typing import Dict, Any, List

import requests
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import permissions, status
from rest_framework.generics import RetrieveUpdateAPIView, ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.module.zoho.models import (
    ZohoCredentials,
    ZohoVendor,
    ZohoChartOfAccount,
    ZohoTaxes,
    ZohoTdsTcs,
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
)
from apps.organizations.models import Organization


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

        # Direct API call to Zoho Books
        url = f"https://www.zohoapis.in/books/v3/contacts"
        headers = {
            'Authorization': f'Zoho-oauthtoken {creds.accessToken}',
        }
        params = {
            'organization_id': creds.organisationId,
            'filter_by': 'Status.All'
        }

        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            parsed_data = response.json()
        except requests.RequestException as e:
            return Response({"detail": f"Failed to fetch contacts from Zoho: {str(e)}"}, status=502)
        except json.JSONDecodeError:
            return Response({"detail": "Failed to parse contacts data from Zoho"}, status=502)

        # Filter contacts to get only vendors
        contacts = [
            contact for contact in parsed_data.get("contacts", [])
            if contact.get("contact_type") == "vendor"
        ]

        # Get existing vendor IDs
        existing_ids = set(
            ZohoVendor.objects.filter(
                organization=org,
                contactId__in=[contact.get("contact_id") for contact in contacts]
            ).values_list("contactId", flat=True)
        )

        # Prepare new vendors for creation
        to_create = []
        for contact in contacts:
            contact_id = contact.get("contact_id")
            if contact_id and contact_id not in existing_ids:
                to_create.append(
                    ZohoVendor(
                        organization=org,
                        contactId=contact_id,
                        companyName=contact.get("company_name") or "",
                        gstNo=contact.get("gst_no") or "",
                    )
                )

        # Bulk create new vendors
        if to_create:
            ZohoVendor.objects.bulk_create(to_create, ignore_conflicts=True)

        return Response({
            "created": len(to_create),
            "total_vendors_seen": len(contacts),
            "message": f"{len(to_create)} new vendors saved successfully." if to_create else "No new vendors to save."
        })


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

        # Direct API call to Zoho Books
        url = f"https://www.zohoapis.in/books/v3/chartofaccounts?organization_id={creds.organisationId}"
        headers = {
            'Authorization': f'Zoho-oauthtoken {creds.accessToken}',
        }
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            parsed_data = response.json()
        except requests.RequestException as e:
            return Response({"detail": f"Failed to fetch chart of accounts from Zoho: {str(e)}"}, status=502)
        except json.JSONDecodeError:
            return Response({"detail": "Failed to parse chart of accounts data"}, status=502)

        chartOfAccounts = parsed_data.get("chartofaccounts", [])

        # Get existing account IDs
        existing_accounts = set(
            ZohoChartOfAccount.objects.filter(
                organization=org,
                accountId__in=[account.get("account_id") for account in chartOfAccounts]
            ).values_list("accountId", flat=True)
        )

        # Prepare new accounts for creation
        to_create = []
        for account in chartOfAccounts:
            account_id = account.get("account_id")
            if account_id and account_id not in existing_accounts:
                to_create.append(
                    ZohoChartOfAccount(
                        organization=org,
                        accountId=account_id,
                        accountName=account.get("account_name") or "",
                    )
                )

        # Bulk create new accounts
        if to_create:
            ZohoChartOfAccount.objects.bulk_create(to_create, ignore_conflicts=True)

        return Response({
            "created": len(to_create),
            "total_accounts_seen": len(chartOfAccounts),
            "message": f"{len(to_create)} new chart of accounts saved successfully." if to_create else "No new chart of accounts to save."
        })


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

        # Direct API call to Zoho Books
        url = f"https://www.zohoapis.in/books/v3/settings/taxes?organization_id={creds.organisationId}"
        headers = {
            'Authorization': f'Zoho-oauthtoken {creds.accessToken}',
        }

        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            parsed_data = response.json()
        except requests.RequestException as e:
            return Response({"detail": f"Failed to fetch taxes from Zoho: {str(e)}"}, status=502)
        except json.JSONDecodeError:
            return Response({"detail": "Failed to parse taxes data"}, status=502)

        zohoTax = parsed_data.get("taxes", [])

        # Get existing tax IDs
        existing_taxes = set(
            ZohoTaxes.objects.filter(
                organization=org,
                taxId__in=[tax.get("tax_id") for tax in zohoTax]
            ).values_list("taxId", flat=True)
        )

        # Prepare new taxes for creation
        to_create = []
        for tax in zohoTax:
            tax_id = tax.get("tax_id")
            if tax_id and tax_id not in existing_taxes:
                to_create.append(
                    ZohoTaxes(
                        organization=org,
                        taxId=tax_id,
                        taxName=tax.get("tax_name") or "",
                    )
                )

        # Bulk create new taxes
        if to_create:
            ZohoTaxes.objects.bulk_create(to_create, ignore_conflicts=True)

        return Response({
            "created": len(to_create),
            "total_taxes_seen": len(zohoTax),
            "message": f"{len(to_create)} new taxes saved successfully." if to_create else "No new taxes to save."
        })


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

        headers = {
            'Authorization': f'Zoho-oauthtoken {creds.accessToken}',
        }

        # Fetch TDS taxes
        tds_url = f"https://www.zohoapis.in/books/v3/settings/taxes"
        tds_params = {
            'organization_id': creds.organisationId,
            'is_tds_request': 'true'
        }

        try:
            tds_response = requests.get(tds_url, headers=headers, params=tds_params, timeout=30)
            tds_response.raise_for_status()
            tds_parsed_data = tds_response.json()
        except requests.RequestException as e:
            return Response({"detail": f"Failed to fetch TDS taxes from Zoho: {str(e)}"}, status=502)
        except json.JSONDecodeError:
            return Response({"detail": "Failed to parse TDS taxes data"}, status=502)

        tds_taxes = tds_parsed_data.get("taxes", [])

        # Get existing TDS tax IDs
        existing_tds_taxes = set(
            ZohoTdsTcs.objects.filter(
                organization=org,
                taxType="TDS",
                taxId__in=[tax.get("tax_id") for tax in tds_taxes]
            ).values_list("taxId", flat=True)
        )

        # Prepare new TDS taxes for creation
        new_tds_taxes = []
        for tax in tds_taxes:
            tax_id = tax.get("tax_id")
            if tax_id and tax_id not in existing_tds_taxes:
                new_tds_taxes.append(
                    ZohoTdsTcs(
                        organization=org,
                        taxId=tax_id,
                        taxName=tax.get("tax_name") or "",
                        taxPercentage=str(tax.get("tax_percentage") or "0"),
                        taxType="TDS",
                    )
                )

        # Fetch TCS taxes
        tcs_url = f"https://www.zohoapis.in/books/v3/settings/taxes"
        tcs_params = {
            'organization_id': creds.organisationId,
            'is_tcs_request': 'true',
            'filter_by': 'Taxes.All'
        }

        try:
            tcs_response = requests.get(tcs_url, headers=headers, params=tcs_params, timeout=30)
            tcs_response.raise_for_status()
            tcs_parsed_data = tcs_response.json()
        except requests.RequestException as e:
            return Response({"detail": f"Failed to fetch TCS taxes from Zoho: {str(e)}"}, status=502)
        except json.JSONDecodeError:
            return Response({"detail": "Failed to parse TCS taxes data"}, status=502)

        tcs_taxes = tcs_parsed_data.get("taxes", [])

        # Get existing TCS tax IDs
        existing_tcs_taxes = set(
            ZohoTdsTcs.objects.filter(
                organization=org,
                taxType="TCS",
                taxId__in=[tax.get("tax_id") for tax in tcs_taxes]
            ).values_list("taxId", flat=True)
        )

        # Prepare new TCS taxes for creation
        new_tcs_taxes = []
        for tax in tcs_taxes:
            tax_id = tax.get("tax_id")
            if tax_id and tax_id not in existing_tcs_taxes:
                new_tcs_taxes.append(
                    ZohoTdsTcs(
                        organization=org,
                        taxId=tax_id,
                        taxName=tax.get("tax_name") or "",
                        taxPercentage=str(tax.get("tax_percentage") or "0"),
                        taxType="TCS",
                    )
                )

        # Bulk create both TDS and TCS taxes
        total_created = 0
        if new_tds_taxes:
            ZohoTdsTcs.objects.bulk_create(new_tds_taxes, ignore_conflicts=True)
            total_created += len(new_tds_taxes)
        if new_tcs_taxes:
            ZohoTdsTcs.objects.bulk_create(new_tcs_taxes, ignore_conflicts=True)
            total_created += len(new_tcs_taxes)

        return Response({
            "created_tds": len(new_tds_taxes),
            "created_tcs": len(new_tcs_taxes),
            "total_tds_seen": len(tds_taxes),
            "total_tcs_seen": len(tcs_taxes),
            "message": "TDS/TCS taxes saved successfully." if total_created > 0 else "No new TDS/TCS taxes to save."
        })


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
