from __future__ import annotations
import base64
import json
from io import BytesIO
from typing import List, Dict, Any, Tuple

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.shortcuts import get_object_or_404

import requests
from pdf2image import convert_from_bytes

from rest_framework import permissions, status
from rest_framework.generics import ListCreateAPIView, RetrieveAPIView
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema, OpenApiParameter

from apps.organizations.models import Organization
from apps.module.zoho.models import (
    ExpenseBill,
    ExpenseZohoBill,
    ExpenseZohoProduct,
    ZohoVendor,
    ZohoChartOfAccount,
    ZohoCredentials,
)
from apps.module.zoho.permissions import IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled
from apps.module.zoho.serializers.expense_bills import (
    ExpenseBillUploadSerializer,
    ExpenseBillSerializer,
    ExpenseBillDetailSerializer,
    ExpenseZohoBillSerializer,
    ExpenseZohoProductSerializer,
    ExpenseBillVerifySerializer,
    ExpenseUploadResultSerializer,
)
from apps.module.zoho.serializers.base import SyncResultSerializer  # reuse shared one


# -------------------- Helpers --------------------

def _get_org(org_id):
    """
    Get organization by ID, supporting UUID.
    """
    return get_object_or_404(Organization, id=org_id)


def _get_org_creds(org: Organization) -> ZohoCredentials:
    return get_object_or_404(ZohoCredentials, organization=org)


def _openai_analyse_images(image_bytes_list: List[bytes]) -> Dict[str, Any]:
    """
    Sends one or many images to OpenAI to extract invoice-like expense data.
    """
    api_key = getattr(settings, "OPENAI_API_KEY", None)
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")

    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    schema = {
        "$schema": "http://json-schema.org/draft/2020-12/schema",
        "title": "Expense",
        "type": "object",
        "properties": {
            "invoiceNumber": {"type": "string"},
            "dateIssued": {"type": "string"},
            "dueDate": {"type": "string"},
            "from": {"type": "object", "properties": {"name": {"type": "string"}, "address": {"type": "string"}}},
            "to": {"type": "object", "properties": {"name": {"type": "string"}, "address": {"type": "string"}}},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"description": {"type": "string"}, "quantity": {"type": "number"}, "price": {"type": "number"}},
                },
            },
            "total": {"type": "number"},
            "igst": {"type": "number"},
            "cgst": {"type": "number"},
            "sgst": {"type": "number"},
        },
    }

    content = [{"type": "text", "text": "Provide a JSON for this expense using this JSON Schema: " + json.dumps(schema)}]
    for img in image_bytes_list:
        b64 = base64.b64encode(img).decode("utf-8")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    prompt = [{"role": "user", "content": content}]
    resp = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=prompt,
        max_tokens=1200,
    )
    return json.loads(resp.choices[0].message.content)


def _pdf_to_all_pages_jpegs(data: bytes) -> List[bytes]:
    images = convert_from_bytes(data)
    out: List[bytes] = []
    for im in images:
        buf = BytesIO()
        im.save(buf, format="JPEG")
        out.append(buf.getvalue())
    return out


def _file_to_pages(request_file) -> Tuple[List[bytes], bool]:
    name = getattr(request_file, "name", "").lower()
    data = request_file.read()
    request_file.seek(0)
    if name.endswith(".pdf"):
        return _pdf_to_all_pages_jpegs(data), True
    return [data], False


def _match_vendor_by_name(org: Organization, company_name: str) -> ZohoVendor | None:
    if not company_name:
        return None
    try:
        from django.db.models.functions import Lower
        return (
            ZohoVendor.objects.filter(organization=org)
            .annotate(lower_name=Lower("companyName"))
            .filter(lower_name=company_name.strip().lower())
            .first()
        )
    except Exception:
        return None


def _persist_analysis_to_models(org: Organization, bill: ExpenseBill, analysed_json: Dict[str, Any]) -> None:
    """
    Save analysed header + items into ExpenseZohoBill & ExpenseZohoProduct.
    """
    bill.analysed_data = analysed_json
    bill.status = "Analysed"
    bill.process = True
    bill.save(update_fields=["analysed_data", "status", "process"])

    invoice_no = analysed_json.get("invoiceNumber", "")
    date_issued = analysed_json.get("dateIssued") or None
    igst = analysed_json.get("igst", 0)
    cgst = analysed_json.get("cgst", 0)
    sgst = analysed_json.get("sgst", 0)
    total = analysed_json.get("total", 0)

    vendor_name = (analysed_json.get("from") or {}).get("name", "")
    vendor = _match_vendor_by_name(org, vendor_name)

    zb = ExpenseZohoBill.objects.create(
        organization=org,
        selectBill=bill,
        vendor=vendor,
        bill_no=invoice_no,
        bill_date=date_issued,
        igst=igst or 0,
        cgst=cgst or 0,
        sgst=sgst or 0,
        total=total or 0,
        note="Analysed Expense",
    )

    for item in (analysed_json.get("items") or []):
        desc = item.get("description", "")
        qty = item.get("quantity", 0)
        price = item.get("price", 0)
        ExpenseZohoProduct.objects.create(
            organization=org,
            zohoBill=zb,
            item_details=desc,
            amount=str(price * qty) if isinstance(price, (int, float)) and isinstance(qty, (int, float)) else "",
            # chart_of_accounts & vendor remain NULL until verification
        )


def _refresh_zoho_access_token(creds: ZohoCredentials) -> str | None:
    if not creds.refreshToken:
        return None
    url = (
        "https://accounts.zoho.in/oauth/v2/token"
        f"?refresh_token={creds.refreshToken}&client_id={creds.clientId}"
        f"&client_secret={creds.clientSecret}&grant_type=refresh_token"
    )
    r = requests.post(url, timeout=30)
    if r.status_code == 200:
        at = r.json().get("access_token")
        if at:
            creds.accessToken = at
            creds.save(update_fields=["accessToken"])
            return at
    return None


# -------------------- Views --------------------

@extend_schema(
    tags=["Zoho Expense Bills"],
    parameters=[OpenApiParameter(name="status", required=False, type=str, location=OpenApiParameter.QUERY)],
    request=ExpenseBillUploadSerializer,
    responses={200: ExpenseUploadResultSerializer, 201: ExpenseUploadResultSerializer},
)
class ExpenseBillListCreateView(ListCreateAPIView):
    """
    GET: List expense bills for the org; filter by status if provided.
    POST: Upload & analyse (Single=all pages; Multiple=each page separately).
          On AI failure, status stays "Draft".
    """
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = ExpenseBillSerializer
    queryset = ExpenseBill.objects.none()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ExpenseBill.objects.none()
        org_id = self.kwargs["org_id"]
        qs = ExpenseBill.objects.filter(organization_id=org_id).order_by("-created_at")
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ExpenseBillUploadSerializer
        return ExpenseBillSerializer

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        ser = ExpenseBillUploadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        org = _get_org(kwargs["org_id"])
        file_type = ser.validated_data.get("fileType") or "Single Invoice/File"
        created = []

        # Handle both single file and multiple files cases
        if "file" in ser.validated_data:
            # Single file upload via 'file' field
            single_file = ser.validated_data["file"]
            created.extend(self._process_file(org, single_file, file_type))
        elif "files" in ser.validated_data:
            # Multiple files upload via 'files' field
            files = ser.validated_data["files"]
            for uploaded_file in files:
                created.extend(self._process_file(org, uploaded_file, file_type))

        out = ExpenseUploadResultSerializer({
            "created": len(created),
            "bills": ExpenseBillSerializer(created, many=True).data
        }).data
        return Response(out, status=status.HTTP_201_CREATED)

    def _process_file(self, org, file, file_type):
        """
        Process a single file for expense bill creation and analysis.
        Returns a list of created ExpenseBill objects.
        """
        created = []
        pages, is_pdf = _file_to_pages(file)

        if file_type == "Single Invoice/File":
            # Process as a single invoice
            bill = ExpenseBill.objects.create(
                organization=org,
                file=file,
                fileType=file_type,
                status="Draft"
            )
            try:
                # Analyze all pages as one document
                analysed_json = _openai_analyse_images(pages)
                _persist_analysis_to_models(org, bill, analysed_json)
            except Exception:
                # If analysis fails, leave it as Draft
                pass
            created.append(bill)
        else:
            # Multiple Invoice/File handling
            if not is_pdf or len(pages) == 1:
                # Non-PDF files or single-page PDFs are handled as one bill
                bill = ExpenseBill.objects.create(
                    organization=org,
                    file=file,
                    fileType=file_type,
                    status="Draft"
                )
                try:
                    analysed_json = _openai_analyse_images(pages)
                    _persist_analysis_to_models(org, bill, analysed_json)
                except Exception:
                    pass
                created.append(bill)
            else:
                # Multi-page PDFs are split into separate bills
                for idx, jpeg in enumerate(pages, start=1):
                    page_name = f"expense-page-{idx}.jpg"
                    bill = ExpenseBill.objects.create(
                        organization=org,
                        file=ContentFile(jpeg, name=page_name),
                        fileType=file_type,
                        status="Draft"
                    )
                    try:
                        # Analyze each page as a separate invoice
                        analysed_json = _openai_analyse_images([jpeg])
                        _persist_analysis_to_models(org, bill, analysed_json)
                    except Exception:
                        pass
                    created.append(bill)

        return created


@extend_schema(tags=["Zoho Expense Bills"], responses=ExpenseBillDetailSerializer)
class ExpenseBillDetailView(RetrieveAPIView):
    """
    Expense bill detail + analysed data (if present).
    """
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = ExpenseBillDetailSerializer
    queryset = ExpenseBill.objects.none()

    def retrieve(self, request, *args, **kwargs):
        org_id = kwargs["org_id"]
        bill = get_object_or_404(ExpenseBill, id=kwargs["bill_id"], organization_id=org_id)
        try:
            zb = ExpenseZohoBill.objects.select_related("vendor").prefetch_related("products").get(
                selectBill=bill, organization_id=org_id
            )
            data = {"bill": ExpenseBillSerializer(bill).data, "analysed": ExpenseZohoBillSerializer(zb).data}
        except ExpenseZohoBill.DoesNotExist:
            data = {"bill": ExpenseBillSerializer(bill).data, "analysed": None}
        return Response(data)


@extend_schema(tags=["Zoho Expense Bills"], request=ExpenseBillVerifySerializer, responses=ExpenseZohoBillSerializer)
class ExpenseBillVerifyView(APIView):
    """
    Update analysed expense header & product details; set bill -> Verified.
    """
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = ExpenseBillVerifySerializer

    @transaction.atomic
    def post(self, request, org_id: int, bill_id, *args, **kwargs):
        bill = get_object_or_404(ExpenseBill, id=bill_id, organization_id=org_id)
        zb = get_object_or_404(ExpenseZohoBill, selectBill=bill, organization_id=org_id)

        ser = ExpenseBillVerifySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        for field in ["vendor", "note", "bill_no", "bill_date", "cgst", "sgst", "igst", "total"]:
            if field in data:
                setattr(zb, field, data[field])
        zb.save()

        updates = {str(p["id"]): p for p in data.get("products", [])}
        if updates:
            for prod in ExpenseZohoProduct.objects.filter(zohoBill=zb):
                p = updates.get(str(prod.id))
                if not p:
                    continue
                if "chart_of_accounts" in p:
                    prod.chart_of_accounts = p["chart_of_accounts"]
                if "vendor" in p:
                    prod.vendor = p["vendor"]
                if "amount" in p:
                    prod.amount = p["amount"]
                if "debit_or_credit" in p:
                    prod.debit_or_credit = p["debit_or_credit"]
                prod.save()

        bill.status = "Verified"
        bill.save(update_fields=["status"])

        return Response(ExpenseZohoBillSerializer(zb).data, status=status.HTTP_200_OK)


@extend_schema(tags=["Zoho Expense Bills"], request=None, responses=SyncResultSerializer)
class ExpenseBillSyncView(APIView):
    """
    Create a Journal in Zoho Books from the verified/analysed expense bill.
    """
    permission_classes = [permissions.IsAuthenticated | IsOrgAdminOrOrgAPIKey, ModuleZohoEnabled]
    serializer_class = SyncResultSerializer

    def post(self, request, org_id: int, bill_id, *args, **kwargs):
        org = _get_org(org_id)
        bill = get_object_or_404(ExpenseBill, id=bill_id, organization=org)
        if bill.status not in ("Verified", "Analysed"):
            return Response({"synced": False, "message": "Bill is not verified/analysed."}, status=400)

        try:
            zbill = ExpenseZohoBill.objects.select_related("vendor").get(
                selectBill=bill, organization=org
            )
        except ExpenseZohoBill.DoesNotExist:
            return Response({"synced": False, "message": "Analysed bill not found."}, status=404)

        products = list(ExpenseZohoProduct.objects.select_related("chart_of_accounts", "vendor").filter(zohoBill=zbill))

        if not products:
            return Response({"synced": False, "message": "No expense items found to sync."}, status=400)

        # Check that all products have the required fields
        missing_fields = []
        for idx, item in enumerate(products):
            if not item.chart_of_accounts:
                missing_fields.append(f"Item {idx+1}: Missing chart of accounts")
            if not item.amount:
                missing_fields.append(f"Item {idx+1}: Missing amount")

        if missing_fields:
            return Response({
                "synced": False,
                "message": "Missing required fields: " + ", ".join(missing_fields)
            }, status=400)

        # Prepare journal entry payload for Zoho
        bill_date_str = zbill.bill_date.isoformat() if zbill.bill_date else None
        journal_date = bill_date_str or None
        reference_number = zbill.bill_no or f"Expense-{bill.billmunshiName}"
        notes = zbill.note or f"Expense entry from bill {bill.billmunshiName}"

        payload = {
            "journal_date": journal_date,
            "reference_number": reference_number,
            "notes": notes,
            "line_items": []
        }

        # Add line items from the expense products
        for item in products:
            line_item = {
                "account_id": str(item.chart_of_accounts.accountId),
                "description": item.item_details or "",
                "debit_or_credit": item.debit_or_credit or "debit",
                "amount": float(item.amount or 0)
            }

            # If vendor is specified, add it to the line item
            if item.vendor:
                line_item["entity_type"] = "vendor"
                line_item["entity_id"] = item.vendor.contactId

            payload["line_items"].append(line_item)

        # Get Zoho credentials and make the API call
        creds = _get_org_creds(org)
        url = f"https://www.zohoapis.in/books/v3/journals?organization_id={creds.organisationId}"
        headers = {"Authorization": f"Zoho-oauthtoken {creds.accessToken}", "Content-Type": "application/json"}

        r = requests.post(url, headers=headers, json=payload, timeout=60)

        # Handle token refresh if needed
        if r.status_code == 401:
            new_at = _refresh_zoho_access_token(creds)
            if new_at:
                headers["Authorization"] = f"Zoho-oauthtoken {new_at}"
                r = requests.post(url, headers=headers, json=payload, timeout=60)

        # Check response and update bill status
        if r.status_code in (200, 201):
            bill.status = "Synced"
            bill.save(update_fields=["status"])
            return Response({"synced": True, "message": "Expense bill synced as journal entry successfully."})

        # Handle errors
        try:
            err = r.json().get("message", r.text)
        except Exception:
            err = r.text

        return Response({"synced": False, "message": f"Zoho error: {err}"}, status=502)
