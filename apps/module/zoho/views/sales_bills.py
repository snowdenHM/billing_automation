"""Sales invoices for Zoho Books — upload, analyse, verify and sync.

Structural mirror of ``vendor_bills``. The two stacks do the same work on
opposite sides of the ledger, so keeping them shaped alike is deliberate:
a fix that applies to one almost always applies to the other.

What actually differs:

  * **The party is a customer.** ``SalesZohoBill.customer`` points at a
    :class:`ZohoCustomer`, synced from Zoho's ``contacts`` endpoint
    filtered to ``contact_type=customer``. A vendor id is not accepted on
    an invoice, so the two masters cannot be merged.

  * **The party comes from the other half of the document.** On a
    purchase bill the organisation is the recipient (``to``) and the
    vendor is the issuer (``from``). A sales invoice is the same document
    read from the other end: the organisation issued it, so ownership is
    checked against ``from`` and the party is taken from ``to``.

  * **The Zoho object is an invoice, not a bill.** The sync POSTs to
    ``/books/v3/invoices`` with ``customer_id`` / ``invoice_number``
    where the vendor feed posts ``vendor_id`` / ``bill_number``. The
    line-item shape (``account_id``, ``name``, ``description``, ``rate``,
    ``quantity``, ``tax_id``) and the discount / adjustment fields are
    the same on both objects.

  * **GST is output tax.** Nothing here enforces it; which ledger is the
    right one is the organisation's own chart of accounts.
"""

import json
import logging

import requests
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common import roles
from apps.common.models import BillActivity
from apps.common.services.activity import (
    BILL_FIELD_LABELS, diff_fields, record_activity, snapshot_bill_fields,
)
from apps.common.services import approval as approval_service
from apps.common.permissions import HasOrgPermission, requires

from apps.common.services.bill_analysis import analyze_bill_bytes, get_sales_bill_prompt
from apps.common.services.duplicate_detection import check_duplicate_bill as _check_duplicate_bill_generic
from apps.common.services.pdf_processing import split_pdf_to_bills
from apps.common.utils import get_organization_from_request

from ..models import (
    SalesBill,
    SalesZohoBill,
    SalesZohoProduct,
    SalesZohoConsolidatedProduct,
    ZohoChartOfAccount,
    ZohoCustomer,
    ZohoTaxes,
)
from ..serializers.common import AnalysisResponseSerializer
from ..serializers.sales_bills import (
    SalesZohoBillSerializer,
    ZohoSalesBillSerializer,
    ZohoSalesBillDetailSerializer,
    ZohoSalesBillMultipleUploadSerializer,
)
from .bill_view_helpers import (
    zoho_bills_list_base,
    zoho_bills_upload_base,
    zoho_bill_detail_base,
    zoho_bill_analyze_base,
    zoho_bill_delete_base,
)
from .helpers import (
    find_zoho_customer_ledger_enhanced,
    get_zoho_credentials,
    refresh_zoho_access_token,
)

logger = logging.getLogger(__name__)

# Seconds. An HTTP call with no timeout pins a worker indefinitely.
ZOHO_HTTP_TIMEOUT = 30

#: Path only. The host is per-account — see zoho_api_base.
ZOHO_INVOICES_PATH = "invoices"

_ANALYSIS_FALLBACK = {
    "invoiceNumber": "", "dateIssued": "", "dueDate": "",
    "from": {"name": "", "address": "", "gst_number": ""},
    "to": {"name": "", "address": "", "gst_number": ""},
    "items": [], "total": 0, "igst": 0, "cgst": 0, "sgst": 0,
}


# ============================================================================
# Thin helper wrappers
# ============================================================================

def check_duplicate_sales_bill(bill, organization):
    """Wrapper: delegates to generic duplicate detection with SalesBill."""
    return _check_duplicate_bill_generic(bill, organization, SalesBill)


def validate_zoho_sales_bill_ownership(json_data, organization):
    """Does this invoice belong to the organisation?

    Checked against ``from``: the organisation is the *issuer* of a sales
    invoice, where on a purchase bill it is the recipient.
    """
    from apps.common.services.ownership import validate_bill_ownership as _core

    return _core(json_data, organization, check_field='from',
                 allow_empty=False, bill_type='sales invoice')


def process_pdf_splitting_sales(pdf_file, organization, file_type, uploaded_by):
    """Split PDF into per-page sales invoices — delegates to shared service."""
    return split_pdf_to_bills(
        pdf_file, organization, file_type, uploaded_by,
        bill_model=SalesBill, filename_prefix="BM-Sales-Page",
    )


def analyze_sales_bill_with_openai(file_content, file_extension):
    """Analyse a sales invoice. Uses the sales prompt, which tells the model
    the letterhead is the seller and the party is under 'Bill To'."""
    logger.info("Starting sales invoice analysis for file type: %s", file_extension)
    try:
        return analyze_bill_bytes(file_content, file_extension, get_sales_bill_prompt())
    except Exception as e:
        logger.error("Sales invoice analysis failed: %s", e)
        return {**_ANALYSIS_FALLBACK, "error": f"Analysis failed: {e}"}


def create_sales_zoho_objects_from_analysis(bill, analyzed_data, organization):
    """Create ``SalesZohoBill`` + line items from analysed data."""
    from .bill_creation import create_zoho_objects_from_analysis

    try:
        ownership = validate_zoho_sales_bill_ownership(analyzed_data, organization)
        logger.info("Ownership validation for sales invoice %s: %s", bill.id, ownership)
        bill.bill_belong_your_org = ownership.get('is_valid', False)
        bill.description = ownership.get('reason') or bill.description or ''
        bill.save(update_fields=['bill_belong_your_org', 'description'])
    except Exception as e:
        # Ownership is advisory — a failure here must not block the analysis.
        logger.error("Ownership validation failed for sales invoice %s: %s", bill.id, e)

    return create_zoho_objects_from_analysis(
        bill, analyzed_data, organization,
        bill_model=SalesZohoBill,
        product_model=SalesZohoProduct,
        consolidated_model=SalesZohoConsolidatedProduct,
        ledger_type='sales',
        bill_type_label='sales',
        assign_taxes=True,
        party_field='customer',
        party_source='to',
        party_lookup_fn=find_zoho_customer_ledger_enhanced,
        include_rate_quantity=True,
    )


# ============================================================================
# Views that delegate to bill_view_helpers
# ============================================================================

@extend_schema(responses=ZohoSalesBillSerializer(many=True), tags=["Zoho Sales Invoices"], methods=["GET"])
@requires(roles.VIEW_BILLS)
@api_view(['GET'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def sales_bills_list_view(request, org_id):
    """List all sales invoices for the organization with pagination."""
    return zoho_bills_list_base(
        request, org_id,
        bill_model=SalesBill,
        list_serializer=ZohoSalesBillSerializer,
    )


@extend_schema(
    summary="Upload Sales Invoices",
    description="Upload single or multiple sales invoice files (PDF, JPG, PNG).",
    request=ZohoSalesBillMultipleUploadSerializer,
    responses={201: ZohoSalesBillSerializer(many=True)},
    tags=["Zoho Sales Invoices"],
    methods=["POST"],
)
@requires(roles.UPLOAD_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
@parser_classes([MultiPartParser, FormParser])
def sales_bill_upload_view(request, org_id):
    """Handle single or multiple sales invoice uploads with PDF splitting."""
    from ..tasks import enqueue_sales_bill_analysis, split_pdf_bill_sales

    return zoho_bills_upload_base(
        request, org_id,
        bill_model=SalesBill,
        list_serializer=ZohoSalesBillSerializer,
        upload_serializer=ZohoSalesBillMultipleUploadSerializer,
        label='Sales',
        check_duplicate_fn=check_duplicate_sales_bill,
        split_pdf_fn=process_pdf_splitting_sales,
        analyze_fn=analyze_sales_bill_with_openai,
        create_objects_fn=create_sales_zoho_objects_from_analysis,
        enqueue_analysis_fn=enqueue_sales_bill_analysis,
        pdf_split_task_fn=split_pdf_bill_sales,
    )


@extend_schema(responses=ZohoSalesBillDetailSerializer, tags=["Zoho Sales Invoices"], methods=["GET"])
@requires(roles.VIEW_BILLS)
@api_view(['GET'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def sales_bill_detail_view(request, org_id, bill_id):
    """Get sales invoice details including analysis data."""
    return zoho_bill_detail_base(
        request, org_id, bill_id,
        bill_model=SalesBill,
        zoho_bill_model=SalesZohoBill,
        detail_serializer=ZohoSalesBillDetailSerializer,
        label='Sales',
        select_related=['selectBill', 'customer'],
        prefetch_related=[
            'products__chart_of_accounts',
            'products__taxes',
            'consolidated_products__chart_of_accounts',
            'consolidated_products__taxes',
        ],
    )


@extend_schema(responses=AnalysisResponseSerializer, tags=["Zoho Sales Invoices"], methods=["POST"])
@requires(roles.ANALYSE_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def sales_bill_analyze_view(request, org_id, bill_id):
    """Analyze a sales invoice using AI. Changes status from 'Draft' to 'Analysed'."""
    return zoho_bill_analyze_base(
        request, org_id, bill_id,
        bill_model=SalesBill,
        label='Sales',
        check_duplicate_fn=check_duplicate_sales_bill,
        analyze_fn=analyze_sales_bill_with_openai,
        create_objects_fn=create_sales_zoho_objects_from_analysis,
    )


@extend_schema(
    responses={"204": None},
    tags=["Zoho Sales Invoices"],
    methods=["DELETE"],
)
@requires(roles.TRASH_BILLS)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def sales_bill_delete_view(request, org_id, bill_id):
    """Delete a sales invoice and its associated file."""
    return zoho_bill_delete_base(request, org_id, bill_id, bill_model=SalesBill, label='Sales')


# ============================================================================
# Verify
# ============================================================================

@extend_schema(
    request=SalesZohoBillSerializer,
    responses=SalesZohoBillSerializer,
    tags=["Zoho Sales Invoices"],
    methods=["POST"],
)
@requires(roles.VERIFY_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def sales_bill_verify_view(request, org_id, bill_id):
    """Verify and update sales invoice data after analysis."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        payload_bill_id = request.data.get('bill_id', bill_id)
        data = request.data.get('zoho_bill', request.data)

        customer_id = data.get('customer')
        if customer_id and not ZohoCustomer.objects.filter(id=customer_id, organization=organization).exists():
            return Response(
                {"detail": f"Customer with ID {customer_id} does not exist in this organization. "
                           "Please sync customers from Zoho first.",
                 "error_type": "customer_not_found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        bill = SalesBill.objects.alive().get(id=payload_bill_id, organization=organization)

        if bill.status not in ['Analysed', 'Verified']:
            return Response(
                {"detail": "Invoice must be in 'Analysed' or 'Verified' status to save"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            zoho_bill = SalesZohoBill.objects.get(selectBill=bill, organization=organization)
        except SalesZohoBill.DoesNotExist:
            return Response(
                {"detail": "No analysed sales data found. Please analyse the invoice first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            _before_verify = snapshot_bill_fields(zoho_bill)

            serializer = SalesZohoBillSerializer(
                zoho_bill, data=data, partial=True, context={'organization': organization},
            )
            if not serializer.is_valid():
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

            updated = serializer.save()

            products_data = data.get('products')
            if products_data is not None:
                _update_sales_products(updated, products_data, organization)

            consolidate_prod = data.get('consolidate_prod')
            if consolidate_prod:
                _update_sales_consolidated(updated, consolidate_prod, organization)

            bill.status = 'Verified'
            bill.save(update_fields=['status'])

            # The change set is the point of the event: a timeline that
            # only says "verified" cannot answer what the operator altered.
            _changes = diff_fields(
                _before_verify,
                snapshot_bill_fields(updated),
                labels=BILL_FIELD_LABELS,
            )
            record_activity(
                bill, BillActivity.Action.VERIFIED,
                actor=request.user,
                organization=organization,
                summary=(f"Verified with {len(_changes)} change(s)"
                         if _changes else "Verified with no changes"),
                changes=_changes,
            )

        return Response(SalesZohoBillSerializer(updated, context={'organization': organization}).data)

    except SalesBill.DoesNotExist:
        return Response({"detail": "Sales invoice not found"}, status=status.HTTP_404_NOT_FOUND)
    except ValueError as e:
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.error("Sales verify failed: %s", e, exc_info=True)
        return Response({"detail": f"Verification failed: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============================================================================
# Sync — Zoho Books /invoices
# ============================================================================

def build_sales_invoice_payload(zoho_bill):
    """Build the ``POST /books/v3/invoices`` body for an analysed invoice.

    Pure: no request, no network, no database writes beyond the reads it
    needs. That is what makes the sync payload testable without a live
    Zoho org — see the scripted checks in the task notes.

    Returns ``(payload, error)``; ``error`` is a user-facing string when
    the invoice cannot be built, in which case ``payload`` is ``None``.
    """
    if not zoho_bill.customer:
        return None, "No customer is associated with this invoice. Assign a customer before syncing."

    line_items = []

    if zoho_bill.consolidate:
        consolidated = zoho_bill.consolidated_products.first()
        if consolidated is None:
            return None, "Consolidation is on but no consolidated line item exists. Re-verify the invoice."
        item = {
            "name": (consolidated.consolidated_item_name or "Consolidated Items")[:100],
            "description": (consolidated.consolidated_item_details or "Multiple items consolidated")[:2000],
            "rate": float(consolidated.consolidated_rate or consolidated.consolidated_amount or 0),
            "quantity": float(consolidated.total_quantity or 1),
            "unit": "unit",
        }
        if consolidated.chart_of_accounts:
            item["account_id"] = str(consolidated.chart_of_accounts.accountId)
        if consolidated.taxes:
            item["tax_id"] = str(consolidated.taxes.taxId)
        line_items.append(item)
    else:
        for product in zoho_bill.products.all():
            item = {
                "name": (product.item_name or product.item_details or "Item")[:100],
                "description": (product.item_details or product.item_name or "")[:2000],
                "rate": float(product.rate or 0),
                "quantity": float(product.quantity or 0),
                "unit": "unit",
            }
            if product.chart_of_accounts:
                item["account_id"] = str(product.chart_of_accounts.accountId)
            if product.taxes:
                item["tax_id"] = str(product.taxes.taxId)
            line_items.append(item)

    if not line_items:
        return None, "No line items found for this invoice. At least one line item is required to sync."

    payload = {
        "customer_id": str(zoho_bill.customer.contactId),
        "line_items": line_items,
    }

    if zoho_bill.bill_no:
        payload["invoice_number"] = str(zoho_bill.bill_no)
    if zoho_bill.bill_date:
        payload["date"] = zoho_bill.bill_date.strftime('%Y-%m-%d')
    if zoho_bill.due_date:
        payload["due_date"] = zoho_bill.due_date.strftime('%Y-%m-%d')
    if zoho_bill.note:
        payload["notes"] = zoho_bill.note

    # India-only fields. Zoho rejects an unknown gst_treatment outright, so
    # only send what the synced contact actually carries.
    if zoho_bill.customer.gst_treatment:
        payload["gst_treatment"] = zoho_bill.customer.gst_treatment
    if zoho_bill.customer.gstNo:
        payload["gst_no"] = str(zoho_bill.customer.gstNo)
    place_of_supply = zoho_bill.place_of_supply or zoho_bill.customer.place_of_supply
    if place_of_supply:
        payload["place_of_supply"] = place_of_supply

    if zoho_bill.discount and zoho_bill.discount > 0:
        payload["discount_type"] = "entity_level"
        if zoho_bill.discount_type == 'Percentage':
            payload["discount"] = f"{float(zoho_bill.discount):.2f}%"
            payload["is_discount_before_tax"] = True
        else:
            payload["discount"] = float(zoho_bill.discount)
            payload["is_discount_before_tax"] = False

    if zoho_bill.adjustment_amount:
        payload["adjustment"] = float(zoho_bill.adjustment_amount)
        payload["adjustment_description"] = zoho_bill.adjustment_description or "Invoice adjustment"

    return payload, None


@extend_schema(
    responses={"200": {"detail": "Sales invoice synced to Zoho successfully"}},
    tags=["Zoho Sales Invoices"],
    methods=["POST"],
)
@requires(roles.SYNC_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def sales_bill_sync_view(request, org_id, bill_id):
    """Sync a verified sales invoice to Zoho Books as an invoice."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = SalesBill.objects.alive().get(id=bill_id, organization=organization)
    except SalesBill.DoesNotExist:
        return Response({"detail": "Sales invoice not found"}, status=status.HTTP_404_NOT_FOUND)

    # Already posted: Zoho has no idempotency key, so a re-sync would
    # create a second invoice for the same document. Checked before the
    # status gate, which would otherwise mask the remote id.
    if bill.zoho_invoice_id:
        return Response({
            'error': 'Already Synced',
            'detail': 'This invoice already exists in Zoho Books.',
            'zoho_invoice_id': bill.zoho_invoice_id,
        }, status=status.HTTP_409_CONFLICT)

    if bill.status != 'Verified':
        return Response({
            'error': 'Invalid Bill Status',
            'detail': f'Invoice must be in "Verified" status to sync. Current status: {bill.status}',
            'current_status': bill.status,
            'required_status': 'Verified',
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        zoho_bill = SalesZohoBill.objects.get(selectBill=bill, organization=organization)
    except SalesZohoBill.DoesNotExist:
        return Response(
            {"detail": "No analysed sales data found for this invoice. Verify it first."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Nobody may post an invoice the organisation has not approved.
    blocked = approval_service.gate_sync(bill, organization)
    if blocked:
        message, code = blocked
        return Response({
            'error': 'Approval Required',
            'message': message,
            'error_code': code,
        }, status=status.HTTP_409_CONFLICT)

    payload, error = build_sales_invoice_payload(zoho_bill)
    if error:
        return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)

    try:
        current_token = get_zoho_credentials(organization)
    except ValueError as e:
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    url = (
        f"{zoho_api_base(current_token)}/books/v3/invoices"
        f"?organization_id={current_token.organisationId}"
    )
    headers = {
        'Authorization': f'Zoho-oauthtoken {current_token.accessToken}',
        'Content-Type': 'application/json',
    }
    body = json.dumps(payload)

    # Zoho answers synchronously, so unlike the Tally feed -- where the
    # connector confirms later via callback -- the terminal event is
    # recorded inline below. This one marks the attempt, so an attempt
    # that dies mid-flight still leaves a trace.
    record_activity(
        bill, BillActivity.Action.SYNC_REQUESTED,
        actor=request.user, organization=organization,
        summary="Sent to Zoho Books as an invoice",
    )

    try:
        response = requests.post(url, headers=headers, data=body, timeout=ZOHO_HTTP_TIMEOUT)

        if response.status_code == 401:
            new_token = refresh_zoho_access_token(current_token)
            if new_token:
                headers['Authorization'] = f'Zoho-oauthtoken {new_token}'
                response = requests.post(url, headers=headers, data=body, timeout=ZOHO_HTTP_TIMEOUT)

        if response.status_code == 201:
            invoice_id = (response.json().get('invoice') or {}).get('invoice_id')
            bill.zoho_invoice_id = str(invoice_id) if invoice_id else None
            bill.status = 'Synced'
            bill.save(update_fields=['zoho_invoice_id', 'status'])
            record_activity(
                bill, BillActivity.Action.SYNC_CONFIRMED,
                actor=request.user, organization=organization,
                summary="Posted to Zoho Books",
                metadata={"zoho_invoice_id": str(invoice_id or "")},
            )
            return Response({
                "detail": "Sales invoice synced to Zoho successfully",
                "zoho_invoice_id": invoice_id,
            })

        response_json = response.json() if response.content else {}
        error_message = response_json.get("message", "Failed to send invoice to Zoho Books")
        logger.error("[SALES SYNC] Zoho API error %s: %s", response.status_code, error_message)
        record_activity(
            bill, BillActivity.Action.SYNC_FAILED,
            actor=request.user, organization=organization,
            summary=f"Zoho Books refused the invoice: {error_message}"[:255],
            metadata={"status_code": response.status_code, "zoho_message": str(error_message)[:500]},
        )
        return Response({
            'error': 'Zoho Sync Failed',
            'detail': f'Failed to sync invoice to Zoho Books. {error_message}',
            'status_code': response.status_code,
            'zoho_message': error_message,
        }, status=status.HTTP_400_BAD_REQUEST)

    except requests.RequestException as e:
        # The request may still have reached Zoho; say so instead of
        # inviting a blind retry that silently duplicates the invoice.
        logger.error("[SALES SYNC] Network error: %s", e)
        record_activity(
            bill, BillActivity.Action.SYNC_FAILED,
            actor=request.user, organization=organization,
            summary=f"Network error while posting: {e}"[:255],
            metadata={"error": str(e)[:500], "outcome_unknown": True},
        )
        return Response({
            "detail": (
                f"Network error while syncing: {e}. The invoice may or may not "
                "have been created in Zoho Books — check there before retrying."
            ),
        }, status=status.HTTP_504_GATEWAY_TIMEOUT)


# ============================================================================
# Private verify helpers
# ============================================================================

def _validate_org_fk(model, pk, organization, label):
    if pk and not model.objects.filter(id=pk, organization=organization).exists():
        raise ValueError(f"{label} with ID {pk} does not exist in this organization.")
    return pk


def _update_sales_products(zoho_bill, products_data, organization):
    """Update / create / delete invoice line items during verify."""
    existing = {str(p.id): p for p in zoho_bill.products.all()}
    processed = set()

    for product_data in products_data:
        chart_id = _validate_org_fk(
            ZohoChartOfAccount, product_data.get('chart_of_accounts'), organization, "Chart of Account")
        tax_id = _validate_org_fk(
            ZohoTaxes, product_data.get('taxes'), organization, "Tax")

        fields = {
            'item_name': product_data.get('item_name'),
            'item_details': product_data.get('item_details'),
            'chart_of_accounts_id': chart_id,
            'taxes_id': tax_id,
            'rate': product_data.get('rate'),
            'quantity': product_data.get('quantity'),
            'amount': product_data.get('amount'),
        }
        fields = {k: v for k, v in fields.items() if v is not None}

        product_id = str(product_data.get('id') or '')
        if product_id in existing:
            instance = existing[product_id]
            for field, value in fields.items():
                setattr(instance, field, value)
            instance.save()
            processed.add(product_id)
        else:
            created = SalesZohoProduct.objects.create(
                zohoBill=zoho_bill, organization=organization, **fields)
            processed.add(str(created.id))

    removed = set(existing) - processed
    if removed:
        SalesZohoProduct.objects.filter(id__in=removed, zohoBill=zoho_bill).delete()


def _update_sales_consolidated(zoho_bill, consolidate_prod_data, organization):
    """Replace the consolidated line item during verify."""
    zoho_bill.consolidated_products.all().delete()

    for entry in consolidate_prod_data:
        chart_id = _validate_org_fk(
            ZohoChartOfAccount, entry.get('chart_of_accounts'), organization, "Chart of Account")
        tax_id = _validate_org_fk(ZohoTaxes, entry.get('taxes'), organization, "Tax")

        SalesZohoConsolidatedProduct.objects.create(
            zohoBill=zoho_bill,
            organization=organization,
            consolidated_item_name=entry.get('item_name') or "Multiple items consolidated",
            consolidated_item_details=entry.get('item_details') or '',
            total_quantity=entry.get('quantity') or 1,
            consolidated_rate=entry.get('rate') or entry.get('amount') or 0,
            consolidated_amount=entry.get('amount') or 0,
            chart_of_accounts_id=chart_id,
            taxes_id=tax_id,
            original_entries_count=zoho_bill.products.count(),
            consolidation_notes='Created from frontend verification',
        )
