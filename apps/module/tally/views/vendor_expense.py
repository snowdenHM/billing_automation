from __future__ import annotations

import os
from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import status, generics
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.module.tally.models import (
    TallyVendorBill,
    TallyVendorAnalyzedBill,
    TallyVendorAnalyzedProduct,
    TallyExpenseBill,
    TallyExpenseAnalyzedBill,
    TallyExpenseAnalyzedProduct,
    Ledger,
)
from apps.module.tally.serializers.vendor_expense import (
    VendorBillUploadSerializer,
    VendorBillSerializer,
    VendorAnalyzedBillSerializer,
    VendorAnalyzedProductSerializer,
    ExpenseBillUploadSerializer,
    ExpenseBillSerializer,
    ExpenseAnalyzedBillSerializer,
    ExpenseAnalyzedProductSerializer,
)
from apps.module.tally.services.analysis import parse_single_invoice
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
    - file: pdf or image for single upload
    - files: list of pdf or image files for multiple upload
    - gst_type: IGST or CGST_SGST
    Creates one or many TallyVendorBill depending on fileType.
    If AI_ANALYSIS_ENABLED=True, immediately analyses and creates analyzed headers/products.
    """
    serializer_class = VendorBillUploadSerializer
    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [IsAuthenticated]
    queryset = TallyVendorBill.objects.none()

    def create(self, request, *args, **kwargs):
        org_id = self.kwargs.get('org_id')
        org = _org_or_404(org_id)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Get validated data
        file = serializer.validated_data.get('file')
        files = serializer.validated_data.get('files')
        file_type = serializer.validated_data.get('fileType')
        gst_type = serializer.validated_data.get('gst_type')

        created_bills = []

        try:
            with transaction.atomic():
                if file:  # Single file upload
                    bill = TallyVendorBill.objects.create(
                        organization=org,
                        file=file,
                        fileType=file_type,
                        status="Draft"
                    )
                    if AI_ANALYSIS_ENABLED:
                        self.perform_analysis(org, bill, gst_type)
                    created_bills.append(bill)

                elif files:  # Multiple files upload
                    for uploaded_file in files:
                        bill = TallyVendorBill.objects.create(
                            organization=org,
                            file=uploaded_file,
                            fileType=file_type,
                            status="Draft"
                        )
                        if AI_ANALYSIS_ENABLED:
                            self.perform_analysis(org, bill, gst_type)
                        created_bills.append(bill)

        except Exception as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        response_serializer = VendorBillSerializer(created_bills, many=True)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    def perform_analysis(self, org: Organization, bill: TallyVendorBill, gst_type: str):
        if not AI_ANALYSIS_ENABLED:
            return  # keep as Draft, will be visible only if AI is off

        # Single: parse whole doc
        parsed, _pages = parse_single_invoice(bill.file.read(), _is_pdf(bill.file))

        with transaction.atomic():
            # Calculate GST values based on gst_type
            if gst_type == 'IGST':
                igst = parsed.total * 0.18 if parsed.igst is None else parsed.igst
                cgst = 0
                sgst = 0
            else:  # CGST_SGST
                igst = 0
                cgst = parsed.total * 0.09 if parsed.cgst is None else parsed.cgst
                sgst = parsed.total * 0.09 if parsed.sgst is None else parsed.sgst

            header = TallyVendorAnalyzedBill.objects.create(
                organization=org,
                selectBill=bill,
                bill_no=parsed.invoice_number,
                bill_date=None,
                total=parsed.total or 0.0,
                igst=igst,
                cgst=cgst,
                sgst=sgst,
                gst_type=gst_type,
                note="Analysed Bill",
            )

            for li in parsed.items:
                product_gst = li.amount * 0.18 if li.gst is None else li.gst
                if gst_type == 'IGST':
                    product_igst = product_gst
                    product_cgst = 0
                    product_sgst = 0
                else:  # CGST_SGST
                    product_igst = 0
                    product_cgst = product_gst / 2
                    product_sgst = product_gst / 2

                TallyVendorAnalyzedProduct.objects.create(
                    organization=org,
                    vendor_bill_analyzed=header,
                    item_name=li.description,
                    item_details=li.description,
                    price=li.price or 0.0,
                    quantity=li.quantity or 1.0,
                    amount=li.amount or 0.0,
                    product_gst=product_gst,
                    igst=product_igst,
                    cgst=product_cgst,
                    sgst=product_sgst
                )


@extend_schema(
    tags=["Tally / Vendor Bills"],
    parameters=[OpenApiParameter("org_id", str, OpenApiParameter.PATH)],
    responses={200: VendorBillSerializer(many=True)},
)
class VendorBillListView(generics.ListAPIView):
    """
    GET /api/v1/tally/org/{org_id}/vendor-bills/
    Lists all vendor bills for an organization.
    """
    serializer_class = VendorBillSerializer
    permission_classes = [IsAuthenticated]
    queryset = TallyVendorBill.objects.none()

    def get_queryset(self):
        org_id = self.kwargs.get('org_id')
        return TallyVendorBill.objects.filter(
            organization_id=org_id
        ).order_by('-created_at')


@extend_schema(
    tags=["Tally / Vendor Bills"],
    parameters=[
        OpenApiParameter("org_id", str, OpenApiParameter.PATH),
        OpenApiParameter("bill_id", str, OpenApiParameter.PATH),
    ],
    responses={200: VendorBillSerializer},
)
class VendorBillDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET /api/v1/tally/org/{org_id}/vendor-bills/{bill_id}/
    Retrieve, update or delete a vendor bill.
    """
    serializer_class = VendorBillSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        org_id = self.kwargs.get('org_id')
        return TallyVendorBill.objects.filter(organization_id=org_id)

    def get_object(self):
        org_id = self.kwargs.get('org_id')
        bill_id = self.kwargs.get('bill_id')
        return get_object_or_404(TallyVendorBill, organization_id=org_id, id=bill_id)


@extend_schema(
    tags=["Tally / Vendor Bills"],
    parameters=[
        OpenApiParameter("org_id", str, OpenApiParameter.PATH),
        OpenApiParameter("bill_id", str, OpenApiParameter.PATH),
    ],
    responses={200: VendorBillSerializer},
)
class VendorBillForceAnalyzeView(generics.GenericAPIView):
    """
    POST /api/v1/tally/org/{org_id}/vendor-bills/{bill_id}/analyze/
    Force re-analysis of a vendor bill.
    """
    serializer_class = VendorBillSerializer
    permission_classes = [IsAuthenticated]
    queryset = TallyVendorBill.objects.none()

    def post(self, request, org_id, bill_id, *args, **kwargs):
        bill = get_object_or_404(TallyVendorBill, organization_id=org_id, id=bill_id)

        try:
            with transaction.atomic():
                # Clear existing analysis if any
                bill.analysed_headers.all().delete()
                # Re-analyze the bill
                if AI_ANALYSIS_ENABLED:
                    VendorBillUploadView.perform_analysis(None, _org_or_404(org_id), bill, request.data.get('gst_type', 'IGST'))
                bill.status = "Analysed"
                bill.save()
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(bill)
        return Response(serializer.data)


@extend_schema(
    tags=["Tally / Vendor Bills"],
    parameters=[
        OpenApiParameter("org_id", str, OpenApiParameter.PATH),
        OpenApiParameter("bill_id", str, OpenApiParameter.PATH),
    ],
    responses={200: VendorBillSerializer},
)
class VendorBillVerifyView(generics.GenericAPIView):
    """
    POST /api/v1/tally/org/{org_id}/vendor-bills/{bill_id}/verify/
    Mark a vendor bill as verified.
    """
    serializer_class = VendorBillSerializer
    permission_classes = [IsAuthenticated]
    queryset = TallyVendorBill.objects.none()

    def post(self, request, org_id, bill_id, *args, **kwargs):
        bill = get_object_or_404(TallyVendorBill, organization_id=org_id, id=bill_id)

        if bill.status != "Analysed":
            return Response(
                {"error": "Can only verify bills in 'Analysed' status"},
                status=status.HTTP_400_BAD_REQUEST
            )

        bill.status = "Verified"
        bill.save()

        serializer = self.get_serializer(bill)
        return Response(serializer.data)


@extend_schema(
    tags=["Tally / Vendor Bills"],
    parameters=[
        OpenApiParameter("org_id", str, OpenApiParameter.PATH),
        OpenApiParameter("bill_id", str, OpenApiParameter.PATH),
    ],
    responses={200: VendorBillSerializer},
)
class VendorBillSyncView(generics.GenericAPIView):
    """
    POST /api/v1/tally/org/{org_id}/vendor-bills/{bill_id}/sync/
    Sync a verified vendor bill to the accounting system.
    """
    serializer_class = VendorBillSerializer
    permission_classes = [IsAuthenticated]
    queryset = TallyVendorBill.objects.none()

    def post(self, request, org_id, bill_id, *args, **kwargs):
        bill = get_object_or_404(TallyVendorBill, organization_id=org_id, id=bill_id)

        if bill.status != "Verified":
            return Response(
                {"error": "Can only sync bills in 'Verified' status"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Add your sync logic here
            bill.status = "Synced"
            bill.save()
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(bill)
        return Response(serializer.data)


@extend_schema(
    tags=["Tally / Vendor Bills"],
    parameters=[
        OpenApiParameter("org_id", str, OpenApiParameter.PATH),
        OpenApiParameter("bill_id", str, OpenApiParameter.PATH),
    ],
    responses={200: VendorAnalyzedBillSerializer},
)
class VendorBillAnalyzedDetailView(generics.RetrieveAPIView):
    """
    GET /api/v1/tally/org/{org_id}/vendor-bills/{bill_id}/analyzed/
    Retrieve analyzed data for a vendor bill.
    """
    serializer_class = VendorAnalyzedBillSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        org_id = self.kwargs.get('org_id')
        bill_id = self.kwargs.get('bill_id')
        vendor_bill = get_object_or_404(TallyVendorBill, organization_id=org_id, id=bill_id)

        # Get the most recent analyzed bill for this vendor bill
        analyzed_bill = TallyVendorAnalyzedBill.objects.filter(
            selectBill=vendor_bill,
            organization_id=org_id
        ).order_by('-created_at').first()

        if not analyzed_bill:
            raise Response(
                {"error": "No analyzed data found for this bill"},
                status=status.HTTP_404_NOT_FOUND
            )

        return analyzed_bill


@extend_schema(
    tags=["Tally / Vendor Bills"],
    parameters=[
        OpenApiParameter("org_id", str, OpenApiParameter.PATH),
        OpenApiParameter("bill_id", str, OpenApiParameter.PATH),
    ],
    request=VendorAnalyzedBillSerializer,
    responses={200: VendorAnalyzedBillSerializer},
)
class VendorBillAnalyzedUpdateView(generics.UpdateAPIView):
    """
    PUT/PATCH /api/v1/tally/org/{org_id}/vendor-bills/{bill_id}/analyzed/update/
    Update analyzed data for a vendor bill.
    """
    serializer_class = VendorAnalyzedBillSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        org_id = self.kwargs.get('org_id')
        bill_id = self.kwargs.get('bill_id')
        vendor_bill = get_object_or_404(TallyVendorBill, organization_id=org_id, id=bill_id)

        # Get the most recent analyzed bill for this vendor bill
        analyzed_bill = TallyVendorAnalyzedBill.objects.filter(
            selectBill=vendor_bill,
            organization_id=org_id
        ).order_by('-created_at').first()

        if not analyzed_bill:
            raise Response(
                {"error": "No analyzed data found for this bill"},
                status=status.HTTP_404_NOT_FOUND
            )

        return analyzed_bill

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        instance = self.get_object()

        # Check if the vendor bill is in a status that allows updating analyzed data
        if instance.selectBill.status == "Synced":
            return Response(
                {"error": "Cannot update analyzed data for a synced bill"},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = self.get_serializer(instance, data=request.data, partial=kwargs.get('partial', False))
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        # Update the vendor bill status if it was in Draft status
        if instance.selectBill.status == "Draft":
            instance.selectBill.status = "Analysed"
            instance.selectBill.save()

        return Response(serializer.data)


# =========================================
#          E X P E N S E   B I L L S
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
    Upload one or multiple expense bills.
    """
    serializer_class = ExpenseBillUploadSerializer
    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [IsAuthenticated]
    queryset = TallyExpenseBill.objects.none()

    def create(self, request, *args, **kwargs):
        org_id = self.kwargs.get('org_id')
        org = _org_or_404(org_id)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        file = serializer.validated_data.get('file')
        files = serializer.validated_data.get('files')
        file_type = serializer.validated_data.get('fileType')

        created_bills = []
        try:
            with transaction.atomic():
                if file:  # Single file upload
                    bill = TallyExpenseBill.objects.create(
                        organization=org,
                        file=file,
                        fileType=file_type,
                        status="Draft"
                    )
                    if AI_ANALYSIS_ENABLED:
                        self.perform_analysis(org, bill)
                    created_bills.append(bill)

                elif files:  # Multiple files upload
                    for uploaded_file in files:
                        bill = TallyExpenseBill.objects.create(
                            organization=org,
                            file=uploaded_file,
                            fileType=file_type,
                            status="Draft"
                        )
                        if AI_ANALYSIS_ENABLED:
                            self.perform_analysis(org, bill)
                        created_bills.append(bill)

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        response_serializer = ExpenseBillSerializer(created_bills, many=True)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

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
                total=parsed.total or 0.0,
                note="Analysed Expense Bill",
            )

            # Create expense products - one for the total amount
            TallyExpenseAnalyzedProduct.objects.create(
                organization=org,
                expense_bill=header,
                item_details=f"Total expense from bill {parsed.invoice_number}",
                amount=parsed.total or 0.0,
                debit_or_credit="debit"
            )


@extend_schema(
    tags=["Tally / Expense Bills"],
    parameters=[OpenApiParameter("org_id", str, OpenApiParameter.PATH)],
    responses={200: ExpenseBillSerializer(many=True)},
)
class ExpenseBillListView(generics.ListAPIView):
    """
    GET /api/v1/tally/org/{org_id}/expense-bills/
    List all expense bills for an organization.
    """
    serializer_class = ExpenseBillSerializer
    permission_classes = [IsAuthenticated]
    queryset = TallyExpenseBill.objects.none()

    def get_queryset(self):
        org_id = self.kwargs.get('org_id')
        return TallyExpenseBill.objects.filter(
            organization_id=org_id
        ).order_by('-created_at')


@extend_schema(
    tags=["Tally / Expense Bills"],
    parameters=[
        OpenApiParameter("org_id", str, OpenApiParameter.PATH),
        OpenApiParameter("bill_id", str, OpenApiParameter.PATH),
    ],
    responses={200: ExpenseBillSerializer},
)
class ExpenseBillDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET /api/v1/tally/org/{org_id}/expense-bills/{bill_id}/
    Retrieve, update or delete an expense bill.
    """
    serializer_class = ExpenseBillSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        org_id = self.kwargs.get('org_id')
        return TallyExpenseBill.objects.filter(organization_id=org_id)

    def get_object(self):
        org_id = self.kwargs.get('org_id')
        bill_id = self.kwargs.get('bill_id')
        return get_object_or_404(TallyExpenseBill, organization_id=org_id, id=bill_id)


@extend_schema(
    tags=["Tally / Expense Bills"],
    parameters=[
        OpenApiParameter("org_id", str, OpenApiParameter.PATH),
        OpenApiParameter("bill_id", str, OpenApiParameter.PATH),
    ],
    responses={200: ExpenseBillSerializer},
)
class ExpenseBillForceAnalyzeView(generics.GenericAPIView):
    """
    POST /api/v1/tally/org/{org_id}/expense-bills/{bill_id}/analyze/
    Force re-analysis of an expense bill.
    """
    serializer_class = ExpenseBillSerializer
    permission_classes = [IsAuthenticated]
    queryset = TallyExpenseBill.objects.none()

    def post(self, request, org_id, bill_id, *args, **kwargs):
        bill = get_object_or_404(TallyExpenseBill, organization_id=org_id, id=bill_id)

        try:
            with transaction.atomic():
                # Clear existing analysis if any
                bill.analysed_headers.all().delete()
                # Re-analyze the bill
                if AI_ANALYSIS_ENABLED:
                    ExpenseBillUploadView.perform_analysis(None, _org_or_404(org_id), bill)
                bill.status = "Analysed"
                bill.save()
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(bill)
        return Response(serializer.data)


@extend_schema(
    tags=["Tally / Expense Bills"],
    parameters=[
        OpenApiParameter("org_id", str, OpenApiParameter.PATH),
        OpenApiParameter("bill_id", str, OpenApiParameter.PATH),
    ],
    responses={200: ExpenseBillSerializer},
)
class ExpenseBillVerifyView(generics.GenericAPIView):
    """
    POST /api/v1/tally/org/{org_id}/expense-bills/{bill_id}/verify/
    Mark an expense bill as verified.
    """
    serializer_class = ExpenseBillSerializer
    permission_classes = [IsAuthenticated]
    queryset = TallyExpenseBill.objects.none()

    def post(self, request, org_id, bill_id, *args, **kwargs):
        bill = get_object_or_404(TallyExpenseBill, organization_id=org_id, id=bill_id)

        if bill.status != "Analysed":
            return Response(
                {"error": "Can only verify bills in 'Analysed' status"},
                status=status.HTTP_400_BAD_REQUEST
            )

        bill.status = "Verified"
        bill.save()

        serializer = self.get_serializer(bill)
        return Response(serializer.data)


@extend_schema(
    tags=["Tally / Expense Bills"],
    parameters=[
        OpenApiParameter("org_id", str, OpenApiParameter.PATH),
        OpenApiParameter("bill_id", str, OpenApiParameter.PATH),
    ],
    responses={200: ExpenseBillSerializer},
)
class ExpenseBillSyncView(generics.GenericAPIView):
    """
    POST /api/v1/tally/org/{org_id}/expense-bills/{bill_id}/sync/
    Sync a verified expense bill to the accounting system.
    """
    serializer_class = ExpenseBillSerializer
    permission_classes = [IsAuthenticated]
    queryset = TallyExpenseBill.objects.none()

    def post(self, request, org_id, bill_id, *args, **kwargs):
        bill = get_object_or_404(TallyExpenseBill, organization_id=org_id, id=bill_id)

        if bill.status != "Verified":
            return Response(
                {"error": "Can only sync bills in 'Verified' status"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Add your sync logic here
            bill.status = "Synced"
            bill.save()
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(bill)
        return Response(serializer.data)


@extend_schema(
    tags=["Tally / Expense Bills"],
    parameters=[
        OpenApiParameter("org_id", str, OpenApiParameter.PATH),
        OpenApiParameter("bill_id", str, OpenApiParameter.PATH),
    ],
    responses={200: ExpenseAnalyzedBillSerializer},
)
class ExpenseBillAnalyzedDetailView(generics.RetrieveAPIView):
    """
    GET /api/v1/tally/org/{org_id}/expense-bills/{bill_id}/analyzed/
    Retrieve analyzed data for an expense bill.
    """
    serializer_class = ExpenseAnalyzedBillSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        org_id = self.kwargs.get('org_id')
        bill_id = self.kwargs.get('bill_id')
        expense_bill = get_object_or_404(TallyExpenseBill, organization_id=org_id, id=bill_id)

        # Get the most recent analyzed bill for this expense bill
        analyzed_bill = TallyExpenseAnalyzedBill.objects.filter(
            selectBill=expense_bill,
            organization_id=org_id
        ).order_by('-created_at').first()

        if not analyzed_bill:
            raise Response(
                {"error": "No analyzed data found for this bill"},
                status=status.HTTP_404_NOT_FOUND
            )

        return analyzed_bill


@extend_schema(
    tags=["Tally / Expense Bills"],
    parameters=[
        OpenApiParameter("org_id", str, OpenApiParameter.PATH),
        OpenApiParameter("bill_id", str, OpenApiParameter.PATH),
    ],
    request=ExpenseAnalyzedBillSerializer,
    responses={200: ExpenseAnalyzedBillSerializer},
)
class ExpenseBillAnalyzedUpdateView(generics.UpdateAPIView):
    """
    PUT/PATCH /api/v1/tally/org/{org_id}/expense-bills/{bill_id}/analyzed/update/
    Update analyzed data for an expense bill.
    """
    serializer_class = ExpenseAnalyzedBillSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        org_id = self.kwargs.get('org_id')
        bill_id = self.kwargs.get('bill_id')
        expense_bill = get_object_or_404(TallyExpenseBill, organization_id=org_id, id=bill_id)

        # Get the most recent analyzed bill for this expense bill
        analyzed_bill = TallyExpenseAnalyzedBill.objects.filter(
            selectBill=expense_bill,
            organization_id=org_id
        ).order_by('-created_at').first()

        if not analyzed_bill:
            raise Response(
                {"error": "No analyzed data found for this bill"},
                status=status.HTTP_404_NOT_FOUND
            )

        return analyzed_bill

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        instance = self.get_object()

        # Check if the expense bill is in a status that allows updating analyzed data
        if instance.selectBill.status == "Synced":
            return Response(
                {"error": "Cannot update analyzed data for a synced bill"},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = self.get_serializer(instance, data=request.data, partial=kwargs.get('partial', False))
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        # Update the expense bill status if it was in Draft status
        if instance.selectBill.status == "Draft":
            instance.selectBill.status = "Analysed"
            instance.selectBill.save()

        return Response(serializer.data)
