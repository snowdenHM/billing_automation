from __future__ import annotations

import os
from typing import List

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import status, generics
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.module.tally.models import (
    TallyVendorBill,
    TallyVendorAnalyzedBill,
    TallyVendorAnalyzedProduct,
    TallyExpenseBill,
    TallyExpenseAnalyzedBill,
    TallyExpenseAnalyzedProduct,
)
from apps.module.tally.serializers.vendor_expense import (
    VendorBillUploadSerializer,
    VendorBillSerializer,
    VendorAnalyzedBillSerializer,
    VendorSyncResultSerializer,
    ExpenseBillUploadSerializer,
    ExpenseBillSerializer,
    ExpenseAnalyzedBillSerializer,
    ExpenseSyncResultSerializer,
)
from apps.module.tally.services.analysis import (
    parse_single_invoice,
    split_multi_invoice,
)
from apps.organizations.models import Organization


# -----------------------
# Common helpers/mixins
# -----------------------

def _org_or_404(org_id) -> Organization:
    return get_object_or_404(Organization, id=org_id)


def _is_pdf(uploaded_file) -> bool:
    return os.path.splitext(uploaded_file.name)[1].lower() == ".pdf"


AI_ANALYSIS_ENABLED = getattr(settings, "AI_ANALYSIS_ENABLED", True)


# =========================================
#            V E N D O R   B I L L S
# =========================================

@extend_schema(
    tags=["Tally / Vendor Bills"],
    parameters=[OpenApiParameter("org_id", str, OpenApiParameter.PATH)],
    request=VendorBillUploadSerializer,
    responses={201: VendorBillSerializer(many=True)},
)
class VendorBillUploadView(generics.CreateAPIView):
    """
    POST /api/v1/tally/org/{org_id}/vendor-bills/upload/
    - fileType: "Single Invoice/File" or "Multiple Invoice/File"
    - file: pdf or image
    Creates one or many TallyVendorBill depending on fileType.
    If AI_ANALYSIS_ENABLED=True, immediately analyses and creates analyzed headers/products.
    """
    serializer_class = VendorBillUploadSerializer
    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [IsAuthenticated]
    queryset = TallyVendorBill.objects.none()

    def perform_analysis(self, org: Organization, bill: TallyVendorBill):
        if not AI_ANALYSIS_ENABLED:
            return  # keep as Draft, will be visible only if AI is off
        # Single: parse whole doc
        parsed, _pages = parse_single_invoice(bill.file.read(), _is_pdf(bill.file))
        with transaction.atomic():
            header = TallyVendorAnalyzedBill.objects.create(
                organization=org,
                selectBill=bill,
                bill_no=parsed.invoice_number,
                bill_date=None,
                total=parsed.total or 0.0,
                igst=parsed.igst or 0.0,
                cgst=parsed.cgst or 0.0,
                sgst=parsed.sgst or 0.0,
                note="Analysed Bill",
            )
            for li in parsed.items:
                TallyVendorAnalyzedProduct.objects.create(
                    organization=org,
                    vendor_bill_analyzed=header,
                    item_name=li.description,
                    item_details=li.description,
                    price=li.price,
                    quantity=int(li.quantity or 0),
                    amount=(li.price or 0) * (li.quantity or 0),
                )
            bill.status = "Analysed"
            bill.process = True
            bill.save(update_fields=["status", "process"])

    def create(self, request, *args, **kwargs):
        org = _org_or_404(kwargs.get("org_id"))
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        upload = ser.validated_data

        created: List[TallyVendorBill] = []
        fobj = request.FILES["file"]
        is_pdf = _is_pdf(fobj)
        fobj.seek(0)
        file_bytes = fobj.read()
        fobj.seek(0)

        with transaction.atomic():
            if upload["fileType"] == "Multiple Invoice/File" and is_pdf:
                # each page -> its own bill
                pages = split_multi_invoice(file_bytes)  # returns list of ParsedInvoice (we also need to create bills)
                from pdf2image import convert_from_bytes
                images = convert_from_bytes(file_bytes)
                for idx, img in enumerate(images, start=1):
                    bill = TallyVendorBill.objects.create(
                        organization=org,
                        file=fobj,  # store the original file; alternatively save page image to a separate file
                        fileType="Single Invoice/File",
                        status="Draft",
                    )
                    created.append(bill)
            else:
                bill = TallyVendorBill.objects.create(
                    organization=org,
                    file=fobj,
                    fileType=upload["fileType"],
                    status="Draft",
                )
                created.append(bill)

        # Analyse immediately when enabled
        for bill in created:
            try:
                self.perform_analysis(org, bill)
            except Exception:
                # leave as Draft when analysis fails
                pass

        out = VendorBillSerializer(created, many=True)
        return Response(out.data, status=status.HTTP_201_CREATED)


@extend_schema(
    tags=["Tally / Vendor Bills"],
    parameters=[OpenApiParameter("org_id", str, OpenApiParameter.PATH)],
    responses={200: VendorBillSerializer(many=True)},
)
class VendorBillListView(generics.ListAPIView):
    """
    GET /api/v1/tally/org/{org_id}/vendor-bills/
    Optional ?status=Analysed|Verified|Synced|Draft
    """
    serializer_class = VendorBillSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        org = _org_or_404(self.kwargs.get("org_id"))
        qs = TallyVendorBill.objects.filter(organization=org).order_by("-created_at")
        status_q = self.request.query_params.get("status")
        if status_q:
            qs = qs.filter(status=status_q)
        return qs


@extend_schema(
    tags=["Tally / Vendor Bills"],
    parameters=[OpenApiParameter("org_id", str, OpenApiParameter.PATH),
                OpenApiParameter("bill_id", str, OpenApiParameter.PATH)],
    responses={200: VendorAnalyzedBillSerializer},
)
class VendorBillForceAnalyzeView(APIView):
    """
    POST /api/v1/tally/org/{org_id}/vendor-bills/{bill_id}/analyze/
    Force analysis for a Draft bill.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = VendorAnalyzedBillSerializer

    def post(self, request, org_id, bill_id):
        org = _org_or_404(org_id)
        bill = get_object_or_404(TallyVendorBill, id=bill_id, organization=org)
        from apps.module.tally.views.vendor_expense import VendorBillUploadView
        view = VendorBillUploadView()
        view.perform_analysis(org, bill)
        header = TallyVendorAnalyzedBill.objects.filter(selectBill=bill).prefetch_related("products").first()
        return Response(VendorAnalyzedBillSerializer(header).data, status=status.HTTP_200_OK)


@extend_schema(
    tags=["Tally / Vendor Bills"],
    request=VendorAnalyzedBillSerializer,
    responses={200: VendorAnalyzedBillSerializer},
    parameters=[OpenApiParameter("org_id", str, OpenApiParameter.PATH),
                OpenApiParameter("bill_id", str, OpenApiParameter.PATH)],
)
class VendorBillVerifyView(APIView):
    """
    PATCH /api/v1/tally/org/{org_id}/vendor-bills/{bill_id}/verify/
    Accepts VendorAnalyzedBill (header + products) to finalize mapping and mark bill as Verified.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, org_id, bill_id):
        org = _org_or_404(org_id)
        bill = get_object_or_404(TallyVendorBill, id=bill_id, organization=org)
        header = TallyVendorAnalyzedBill.objects.filter(selectBill=bill).first()
        if not header:
            header = TallyVendorAnalyzedBill.objects.create(organization=org, selectBill=bill)

        ser = VendorAnalyzedBillSerializer(instance=header, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)

        with transaction.atomic():
            header = ser.save()
            # handle nested products manually (replace all if provided)
            products = request.data.get("products", None)
            if products is not None:
                header.products.all().delete()
                for p in products:
                    TallyVendorAnalyzedProduct.objects.create(
                        organization=org,
                        vendor_bill_analyzed=header,
                        item_name=p.get("item_name"),
                        item_details=p.get("item_details"),
                        taxes_id=p.get("taxes"),
                        price=p.get("price"),
                        quantity=p.get("quantity") or 0,
                        amount=p.get("amount"),
                        product_gst=p.get("product_gst"),
                        igst=p.get("igst") or 0,
                        cgst=p.get("cgst") or 0,
                        sgst=p.get("sgst") or 0,
                    )
            bill.status = "Verified"
            bill.save(update_fields=["status"])

        return Response(VendorAnalyzedBillSerializer(header).data, status=status.HTTP_200_OK)


@extend_schema(
    tags=["Tally / Vendor Bills"],
    responses={200: VendorSyncResultSerializer},
    parameters=[OpenApiParameter("org_id", str, OpenApiParameter.PATH),
                OpenApiParameter("bill_id", str, OpenApiParameter.PATH)],
)
class VendorBillSyncView(APIView):
    """
    POST /api/v1/tally/org/{org_id}/vendor-bills/{bill_id}/sync/
    For now we just mark Synced (your TCP process will fetch & push to Tally).
    """
    permission_classes = [IsAuthenticated]
    serializer_class = VendorSyncResultSerializer

    def post(self, request, org_id, bill_id):
        org = _org_or_404(org_id)
        bill = get_object_or_404(TallyVendorBill, id=bill_id, organization=org)
        bill.status = "Synced"
        bill.save(update_fields=["status"])
        return Response({"id": bill.id, "status": bill.status}, status=status.HTTP_200_OK)


# =========================================
#            E X P E N S E   B I L L S
# =========================================

@extend_schema(
    tags=["Tally / Expense Bills"],
    parameters=[OpenApiParameter("org_id", str, OpenApiParameter.PATH)],
    request=ExpenseBillUploadSerializer,
    responses={201: ExpenseBillSerializer(many=True)},
)
class ExpenseBillUploadView(generics.CreateAPIView):
    """
    POST /api/v1/tally/org/{org_id}/expense-bills/upload/
    """
    serializer_class = ExpenseBillUploadSerializer
    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [IsAuthenticated]
    queryset = TallyExpenseBill.objects.none()

    def perform_analysis(self, org: Organization, bill: TallyExpenseBill):
        if not AI_ANALYSIS_ENABLED:
            return
        parsed, _pages = parse_single_invoice(bill.file.read(), _is_pdf(bill.file))
        with transaction.atomic():
            header = TallyExpenseAnalyzedBill.objects.create(
                organization=org,
                selectBill=bill,
                bill_no=parsed.invoice_number,
                bill_date=None,
                total=str(parsed.total or 0.0),
                igst=parsed.igst or 0.0,
                cgst=parsed.cgst or 0.0,
                sgst=parsed.sgst or 0.0,
                note="Analysed Expense",
            )
            # turn items into debit/credit rows as credit by default
            for li in parsed.items:
                TallyExpenseAnalyzedProduct.objects.create(
                    organization=org,
                    expense_bill=header,
                    item_details=li.description,
                    amount=str((li.price or 0) * (li.quantity or 0)),
                    debit_or_credit="credit",
                )
            bill.status = "Analysed"
            bill.process = True
            bill.save(update_fields=["status", "process"])

    def create(self, request, *args, **kwargs):
        org = _org_or_404(kwargs.get("org_id"))
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        upload = ser.validated_data

        created: List[TallyExpenseBill] = []
        fobj = request.FILES["file"]
        is_pdf = _is_pdf(fobj)
        fobj.seek(0)
        file_bytes = fobj.read()
        fobj.seek(0)

        with transaction.atomic():
            if upload["fileType"] == "Multiple Invoice/File" and is_pdf:
                # each page -> its own bill
                from pdf2image import convert_from_bytes
                convert_from_bytes(file_bytes)  # warm up / validate
                # create one bill per page (we keep original file; pages can be stored separately if you prefer)
                # here we just create N bills with same file; your downstream can handle page-splitting or you can extend to store per-page files
                pages_count = len(convert_from_bytes(file_bytes))
                for _ in range(pages_count):
                    bill = TallyExpenseBill.objects.create(
                        organization=org,
                        file=fobj,
                        fileType="Single Invoice/File",
                        status="Draft",
                    )
                    created.append(bill)
            else:
                bill = TallyExpenseBill.objects.create(
                    organization=org,
                    file=fobj,
                    fileType=upload["fileType"],
                    status="Draft",
                )
                created.append(bill)

        for bill in created:
            try:
                self.perform_analysis(org, bill)
            except Exception:
                pass

        out = ExpenseBillSerializer(created, many=True)
        return Response(out.data, status=status.HTTP_201_CREATED)


@extend_schema(
    tags=["Tally / Expense Bills"],
    responses={200: ExpenseBillSerializer(many=True)},
    parameters=[OpenApiParameter("org_id", str, OpenApiParameter.PATH)],
)
class ExpenseBillListView(generics.ListAPIView):
    """
    GET /api/v1/tally/org/{org_id}/expense-bills/
    Optional ?status=Analysed|Verified|Synced|Draft
    """
    serializer_class = ExpenseBillSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        org = _org_or_404(self.kwargs.get("org_id"))
        qs = TallyExpenseBill.objects.filter(organization=org).order_by("-created_at")
        s = self.request.query_params.get("status")
        if s:
            qs = qs.filter(status=s)
        return qs


@extend_schema(
    tags=["Tally / Expense Bills"],
    parameters=[OpenApiParameter("org_id", str, OpenApiParameter.PATH),
                OpenApiParameter("bill_id", str, OpenApiParameter.PATH)],
    responses={200: ExpenseAnalyzedBillSerializer},
)
class ExpenseBillForceAnalyzeView(APIView):
    """
    POST /api/v1/tally/org/{org_id}/expense-bills/{bill_id}/analyze/
    """
    permission_classes = [IsAuthenticated]
    serializer_class = ExpenseAnalyzedBillSerializer

    def post(self, request, org_id, bill_id):
        org = _org_or_404(org_id)
        bill = get_object_or_404(TallyExpenseBill, id=bill_id, organization=org)
        from apps.module.tally.views.vendor_expense import ExpenseBillUploadView
        view = ExpenseBillUploadView()
        view.perform_analysis(org, bill)
        header = TallyExpenseAnalyzedBill.objects.filter(selectBill=bill).prefetch_related("products").first()
        return Response(ExpenseAnalyzedBillSerializer(header).data, status=status.HTTP_200_OK)


@extend_schema(
    tags=["Tally / Expense Bills"],
    request=ExpenseAnalyzedBillSerializer,
    responses={200: ExpenseAnalyzedBillSerializer},
    parameters=[OpenApiParameter("org_id", str, OpenApiParameter.PATH),
                OpenApiParameter("bill_id", str, OpenApiParameter.PATH)],
)
class ExpenseBillVerifyView(APIView):
    """
    PATCH /api/v1/tally/org/{org_id}/expense-bills/{bill_id}/verify/
    Accepts ExpenseAnalyzedBill (header + products) to finalize mapping and mark bill as Verified.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, org_id, bill_id):
        org = _org_or_404(org_id)
        bill = get_object_or_404(TallyExpenseBill, id=bill_id, organization=org)
        header = TallyExpenseAnalyzedBill.objects.filter(selectBill=bill).first()
        if not header:
            header = TallyExpenseAnalyzedBill.objects.create(organization=org, selectBill=bill)

        ser = ExpenseAnalyzedBillSerializer(instance=header, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)

        with transaction.atomic():
            header = ser.save()
            products = request.data.get("products", None)
            if products is not None:
                header.products.all().delete()
                for p in products:
                    TallyExpenseAnalyzedProduct.objects.create(
                        organization=org,
                        expense_bill=header,
                        item_details=p.get("item_details"),
                        chart_of_accounts_id=p.get("chart_of_accounts"),
                        amount=p.get("amount"),
                        debit_or_credit=p.get("debit_or_credit") or "credit",
                    )
            bill.status = "Verified"
            bill.save(update_fields=["status"])

        return Response(ExpenseAnalyzedBillSerializer(header).data, status=status.HTTP_200_OK)


@extend_schema(
    tags=["Tally / Expense Bills"],
    responses={200: ExpenseSyncResultSerializer},
    parameters=[OpenApiParameter("org_id", str, OpenApiParameter.PATH),
                OpenApiParameter("bill_id", str, OpenApiParameter.PATH)],
)
class ExpenseBillSyncView(APIView):
    """
    POST /api/v1/tally/org/{org_id}/expense-bills/{bill_id}/sync/
    """
    permission_classes = [IsAuthenticated]
    serializer_class = ExpenseSyncResultSerializer

    def post(self, request, org_id, bill_id):
        org = _org_or_404(org_id)
        bill = get_object_or_404(TallyExpenseBill, id=bill_id, organization=org)
        bill.status = "Synced"
        bill.save(update_fields=["status"])
        return Response({"id": bill.id, "status": bill.status}, status=status.HTTP_200_OK)
