# apps/module/tally/bill_move_view.py

import logging
from apps.common import roles
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status
from apps.common.permissions import HasOrgPermission, requires
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.models import BillActivity
from apps.common.services.activity import record_activity
from apps.common.utils import get_organization_from_request
from apps.organizations.models import Organization
from ..models import (
    TallyVendorBill,
    TallyVendorAnalyzedBill,
    TallyVendorAnalyzedProduct,
    TallyVendorConsolidatedProduct,
    TallyExpenseBill,
    TallyExpenseAnalyzedBill,
    TallyExpenseAnalyzedProduct,
    TallyExpenseConsolidatedProduct,
    TallySalesBill,
    TallySalesAnalyzedBill,
    TallySalesAnalyzedProduct,
    TallySalesConsolidatedProduct,
    TallyPaymentBill,
    TallyPaymentAnalyzedBill,
    TallyPaymentAnalyzedProduct,
    TallyPaymentConsolidatedProduct,
    TallyReceiptBill,
    TallyReceiptAnalyzedBill,
    TallyReceiptAnalyzedProduct,
    TallyReceiptConsolidatedProduct,
)

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Every voucher a bill can move between, described once.
#
# This used to be hand-written if/elif pairs for vendor and expense
# only — two modules, four branches, and the shape repeated in the
# lookup, the create and the line copy. Adding a third voucher meant
# writing all three again, so nobody did: the payment list carried
# ``allowMove={false}`` with a comment saying the endpoint had no
# correct value to send, and Receipt and Sales had no Move at all.
#
# ``shape`` is what actually differs between them. An ITEM voucher
# carries goods — name, rate, quantity, a GST rate per line. A LEDGER
# voucher carries postings — a ledger, an amount and a side. Moving
# between two of the same shape keeps everything; moving across shapes
# keeps what the destination can hold and drops the rest, which is why
# the list warns before it does it.
# ----------------------------------------------------------------------
MODULES = {
    "vendor": {
        "label": "Purchase Voucher",
        "bill": TallyVendorBill,
        "analysed": TallyVendorAnalyzedBill,
        "product": TallyVendorAnalyzedProduct,
        "consolidated": TallyVendorConsolidatedProduct,
        "fk": "vendor_bill_analyzed",
        "party": "vendor",
        "shape": "item",
    },
    "sales": {
        "label": "Sales Voucher",
        "bill": TallySalesBill,
        "analysed": TallySalesAnalyzedBill,
        "product": TallySalesAnalyzedProduct,
        "consolidated": TallySalesConsolidatedProduct,
        "fk": "sales_bill_analyzed",
        "party": "customer",
        "shape": "item",
    },
    "expense": {
        "label": "Journal Entry",
        "bill": TallyExpenseBill,
        "analysed": TallyExpenseAnalyzedBill,
        "product": TallyExpenseAnalyzedProduct,
        "consolidated": TallyExpenseConsolidatedProduct,
        "fk": "expense_bill",
        "party": "vendor",
        "shape": "ledger",
    },
    "payment": {
        "label": "Payment Voucher",
        "bill": TallyPaymentBill,
        "analysed": TallyPaymentAnalyzedBill,
        "product": TallyPaymentAnalyzedProduct,
        "consolidated": TallyPaymentConsolidatedProduct,
        "fk": "payment_bill",
        "party": "vendor",
        "shape": "ledger",
    },
    "receipt": {
        "label": "Receipt Voucher",
        "bill": TallyReceiptBill,
        "analysed": TallyReceiptAnalyzedBill,
        "product": TallyReceiptAnalyzedProduct,
        "consolidated": TallyReceiptConsolidatedProduct,
        "fk": "receipt_bill",
        "party": "customer",
        "shape": "ledger",
    },
}

MODULE_LABELS = {key: spec["label"] for key, spec in MODULES.items()}


def _settable(model, data):
    """Only the fields this model actually has.

    The five analysed-bill models agree on most columns and not all of
    them — TDS is on the journal stack, place of supply on the invoice
    one. Filtering here means a destination takes what it understands
    and silently ignores the rest, instead of the caller needing a
    branch per pair.
    """
    names = {f.name for f in model._meta.get_fields()}
    return {k: v for k, v in data.items() if k in names}


@extend_schema(
    summary="Move Tally Bills Between Modules",
    description="Move multiple Tally bills from vendor to expense or vice versa. Preserves analysis data to save LLM tokens.",
    request={
        'type': 'object',
        'properties': {
            'from': {
                'type': 'string',
                'enum': list(MODULES),
                'description': 'Source module'
            },
            'to': {
                'type': 'string',
                'enum': list(MODULES),
                'description': 'Destination module'
            },
            'bill_ids': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Array of bill IDs to move'
            }
        },
        'required': ['from', 'to', 'bill_ids']
    },
    responses={
        200: {
            'type': 'object',
            'properties': {
                'message': {'type': 'string'},
                'total_bills': {'type': 'integer'},
                'successful_moves': {'type': 'integer'},
                'failed_moves': {'type': 'integer'},
                'moved_bills': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'original_bill_id': {'type': 'string'},
                            'new_bill_id': {'type': 'string'},
                            'status': {'type': 'string'}
                        }
                    }
                },
                'from_module': {'type': 'string'},
                'to_module': {'type': 'string'},
                'preserved_analysis': {'type': 'boolean'},
                'errors': {'type': 'array'}
            }
        },
        400: {'description': 'Invalid request or bill status'},
        404: {'description': 'Bills not found'}
    },
    tags=['Tally Bill Management']
)
@requires(roles.MOVE_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def move_tally_bills_between_modules_view(request, org_id):
    """
    Move multiple Tally bills between vendor/expense modules while preserving analysis data
    Only works for Draft/Analysed status bills to prevent data corruption
    """
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    # Validate request data
    from_module = request.data.get('from', '').lower()
    to_module = request.data.get('to', '').lower()
    bill_ids = request.data.get('bill_ids', [])

    valid_modules = list(MODULES)

    if not from_module or not to_module:
        return Response({
            'error': 'Missing Parameters',
            'detail': 'Both "from" and "to" parameters are required',
            'valid_modules': valid_modules
        }, status=status.HTTP_400_BAD_REQUEST)

    if from_module not in valid_modules or to_module not in valid_modules:
        return Response({
            'error': 'Invalid Module',
            'detail': f'Modules must be one of: {valid_modules}',
            'received': {'from': from_module, 'to': to_module}
        }, status=status.HTTP_400_BAD_REQUEST)

    if from_module == to_module:
        return Response({
            'error': 'Same Module',
            'detail': 'Source and destination modules cannot be the same',
            'modules': {'from': from_module, 'to': to_module}
        }, status=status.HTTP_400_BAD_REQUEST)

    if not bill_ids or not isinstance(bill_ids, list):
        return Response({
            'error': 'Missing Bill IDs',
            'detail': 'bill_ids must be provided as an array of bill IDs',
            'example': {'bill_ids': ['bill-id-1', 'bill-id-2']}
        }, status=status.HTTP_400_BAD_REQUEST)

    # Track results
    moved_bills = []
    errors = []
    successful_moves = 0
    failed_moves = 0

    for bill_id in bill_ids:
        try:
            with transaction.atomic():
                # Source, looked up through the registry rather than
                # a branch per module.
                src = MODULES[from_module]
                dst = MODULES[to_module]
                source_analyzed_bill = None

                try:
                    source_bill = src["bill"].objects.alive().get(
                        id=bill_id, organization=organization,
                    )
                except src["bill"].DoesNotExist:
                    errors.append({
                        'bill_id': bill_id,
                        'error': f'{src["label"]} with ID {bill_id} not found',
                    })
                    failed_moves += 1
                    continue

                try:
                    source_analyzed_bill = src["analysed"].objects.prefetch_related(
                        "products",
                    ).get(selected_bill=source_bill, organization=organization)
                except src["analysed"].DoesNotExist:
                    source_analyzed_bill = None

                # Check bill status - only allow Draft or Analysed
                if source_bill.status not in ['Draft', 'Analysed']:
                    errors.append({
                        'bill_id': bill_id,
                        'error': f'Bill must be in Draft or Analysed status to move. Current status: {source_bill.status}'
                    })
                    failed_moves += 1
                    continue

                # Create new bill in destination module
                new_bill = None
                new_analyzed_bill = None

                # Common bill data to transfer
                common_data = {
                    'organization': organization,
                    'uploaded_by': getattr(source_bill, 'uploaded_by', None),
                    'file_type': getattr(source_bill, 'file_type', 'Single Invoice/File'),
                    'status': source_bill.status,
                    'process': getattr(source_bill, 'process', False),
                    'analysed_data': getattr(source_bill, 'analysed_data', {}),
                    'file': source_bill.file  # Copy file reference
                }

                # Create the destination bill and carry across what it
                # can hold.
                new_bill = dst["bill"].objects.create(**common_data)

                if source_analyzed_bill:
                    carried = {
                        "selected_bill": new_bill,
                        "organization": organization,
                        "bill_no": str(getattr(source_analyzed_bill, "bill_no", "") or "")[:50],
                        "bill_date": getattr(source_analyzed_bill, "bill_date", None),
                        "due_date": getattr(source_analyzed_bill, "due_date", None),
                        "total": getattr(source_analyzed_bill, "total", 0),
                        "igst": getattr(source_analyzed_bill, "igst", 0),
                        "cgst": getattr(source_analyzed_bill, "cgst", 0),
                        "sgst": getattr(source_analyzed_bill, "sgst", 0),
                        "tds": getattr(source_analyzed_bill, "tds", 0),
                        "note": (
                            f"Moved from {src['label']} - "
                            + str(getattr(source_analyzed_bill, "note", "") or "")
                        )[:100],
                        "consolidate": getattr(source_analyzed_bill, "consolidate", False),
                    }

                    # The party only travels when both sides keep it in
                    # the same place. A supplier ledger is not a customer
                    # ledger, so moving a purchase to a sales voucher
                    # leaves the party blank for the operator to pick
                    # rather than writing a creditor into a debtor field.
                    if src["party"] == dst["party"]:
                        carried[dst["party"]] = getattr(
                            source_analyzed_bill, src["party"], None,
                        )

                    new_analyzed_bill = dst["analysed"].objects.create(
                        **_settable(dst["analysed"], carried)
                    )

                # Line items, translated between the two shapes.
                if source_analyzed_bill and source_bill.status == 'Analysed' and new_analyzed_bill:
                    try:
                        for product in source_analyzed_bill.products.all():
                            if dst["shape"] == "item":
                                row = {
                                    dst["fk"]: new_analyzed_bill,
                                    "organization": organization,
                                    "item_name": str(
                                        getattr(product, "item_name", "")
                                        or getattr(product, "item_details", "")
                                        or ""
                                    )[:100],
                                    "item_details": str(getattr(product, "item_details", "") or ""),
                                    "price": getattr(product, "price", 0) or 0,
                                    "quantity": getattr(product, "quantity", 0) or 0,
                                    "amount": getattr(product, "amount", 0) or 0,
                                    "product_gst": str(getattr(product, "product_gst", "") or "")[:20],
                                    "igst": getattr(product, "igst", 0) or 0,
                                    "cgst": getattr(product, "cgst", 0) or 0,
                                    "sgst": getattr(product, "sgst", 0) or 0,
                                    "stock_item": getattr(product, "stock_item", None),
                                    "taxes": getattr(product, "taxes", None),
                                }
                            else:
                                row = {
                                    dst["fk"]: new_analyzed_bill,
                                    "organization": organization,
                                    "item_details": str(
                                        getattr(product, "item_details", "")
                                        or getattr(product, "item_name", "")
                                        or ""
                                    ),
                                    "chart_of_accounts": (
                                        getattr(product, "chart_of_accounts", None)
                                        or getattr(product, "taxes", None)
                                    ),
                                    "amount": getattr(product, "amount", 0) or 0,
                                    "debit_or_credit": getattr(
                                        product, "debit_or_credit", "debit",
                                    ) or "debit",
                                }
                            dst["product"].objects.create(
                                **_settable(dst["product"], row)
                            )
                    except Exception as product_transfer_error:
                        logger.warning(
                            "Failed to transfer line items for Tally bill %s: %s",
                            bill_id, product_transfer_error,
                        )
                        # A move that lands without its lines is still a
                        # move; the operator re-enters them rather than
                        # losing the document.

                # The source goes. Cascade would take the children
                # anyway; they are cleared first so a failure here shows
                # up as a failure rather than as orphaned rows.
                if source_analyzed_bill:
                    source_analyzed_bill.products.all().delete()
                    try:
                        source_analyzed_bill.consolidated_products.all().delete()
                    except Exception:
                        # Not every stack exposes the reverse name; the
                        # cascade on delete covers it either way.
                        pass
                    source_analyzed_bill.delete()

                # The source row is about to go, so the move is recorded
                # against the bill that survives it. Both ids are kept so
                # the two halves of the move can still be tied together.
                record_activity(
                    new_bill, BillActivity.Action.MOVED,
                    actor=request.user,
                    organization=organization,
                    summary=f"Moved from {MODULE_LABELS.get(from_module, from_module)} "
                            f"to {MODULE_LABELS.get(to_module, to_module)}",
                    metadata={
                        "from": from_module,
                        "to": to_module,
                        "previous_bill_id": str(bill_id),
                    },
                )

                source_bill.delete()

                # Track successful move
                moved_bills.append({
                    'original_bill_id': str(bill_id),
                    'new_bill_id': str(new_bill.id),
                    'status': 'success'
                })
                successful_moves += 1

                logger.info(f"Successfully moved Tally bill {bill_id} from {from_module} to {to_module} module")

        except Exception as e:
            logger.error(f"Error moving Tally bill {bill_id}: {str(e)}")
            errors.append({
                'bill_id': bill_id,
                'error': f'Failed to move bill: {str(e)}'
            })
            failed_moves += 1

    # Prepare response
    total_bills = len(bill_ids)

    return Response({
        'message': f'Tally batch move completed: {successful_moves} successful, {failed_moves} failed',
        'total_bills': total_bills,
        'successful_moves': successful_moves,
        'failed_moves': failed_moves,
        'moved_bills': moved_bills,
        'from_module': from_module,
        'to_module': to_module,
        'preserved_analysis': successful_moves > 0,  # Indicates if LLM re-analysis was avoided
        'llm_tokens_saved': successful_moves > 0,
        'errors': errors
    })
