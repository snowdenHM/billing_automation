# apps/module/tally/bill_move_view.py

import logging
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

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
)

logger = logging.getLogger(__name__)


@extend_schema(
    summary="Move Tally Bills Between Modules",
    description="Move multiple Tally bills from vendor to expense or vice versa. Preserves analysis data to save LLM tokens.",
    request={
        'type': 'object',
        'properties': {
            'from': {
                'type': 'string',
                'enum': ['vendor', 'expense'],
                'description': 'Source module'
            },
            'to': {
                'type': 'string',
                'enum': ['vendor', 'expense'],
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
@api_view(['POST'])
@permission_classes([IsAuthenticated])
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

    valid_modules = ['vendor', 'expense']

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
                # Get source bill and analyzed bill based on from_module
                source_bill = None
                source_analyzed_bill = None

                if from_module == 'vendor':
                    try:
                        source_bill = TallyVendorBill.objects.alive().get(id=bill_id, organization=organization)
                        try:
                            source_analyzed_bill = TallyVendorAnalyzedBill.objects.prefetch_related(
                                'products', 'consolidated_product'
                            ).get(selected_bill=source_bill, organization=organization)
                        except TallyVendorAnalyzedBill.DoesNotExist:
                            source_analyzed_bill = None
                    except TallyVendorBill.DoesNotExist:
                        errors.append({
                            'bill_id': bill_id,
                            'error': f'Vendor bill with ID {bill_id} not found'
                        })
                        failed_moves += 1
                        continue

                elif from_module == 'expense':
                    try:
                        source_bill = TallyExpenseBill.objects.alive().get(id=bill_id, organization=organization)
                        try:
                            source_analyzed_bill = TallyExpenseAnalyzedBill.objects.prefetch_related(
                                'products', 'consolidated_product'
                            ).get(selected_bill=source_bill, organization=organization)
                        except TallyExpenseAnalyzedBill.DoesNotExist:
                            source_analyzed_bill = None
                    except TallyExpenseBill.DoesNotExist:
                        errors.append({
                            'bill_id': bill_id,
                            'error': f'Expense bill with ID {bill_id} not found'
                        })
                        failed_moves += 1
                        continue

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

                # Create bill based on destination module
                if to_module == 'vendor':
                    new_bill = TallyVendorBill.objects.create(**common_data)

                    # Transfer analyzed bill data if exists
                    if source_analyzed_bill:
                        analyzed_common_data = {
                            'selected_bill': new_bill,
                            'organization': organization,
                            'vendor': getattr(source_analyzed_bill, 'vendor', None),
                            'bill_no': str(getattr(source_analyzed_bill, 'bill_no', ''))[:50],
                            'bill_date': getattr(source_analyzed_bill, 'bill_date', None),
                            'due_date': getattr(source_analyzed_bill, 'due_date', None),
                            'total': getattr(source_analyzed_bill, 'total', 0),
                            'igst': getattr(source_analyzed_bill, 'igst', 0),
                            'cgst': getattr(source_analyzed_bill, 'cgst', 0),
                            'sgst': getattr(source_analyzed_bill, 'sgst', 0),
                            'note': (f"Moved from {from_module} - " + str(getattr(source_analyzed_bill, 'note', '')))[:100],
                            'consolidate': getattr(source_analyzed_bill, 'consolidate', False)
                        }

                        new_analyzed_bill = TallyVendorAnalyzedBill.objects.create(**analyzed_common_data)

                elif to_module == 'expense':
                    new_bill = TallyExpenseBill.objects.create(**common_data)

                    if source_analyzed_bill:
                        analyzed_common_data = {
                            'selected_bill': new_bill,
                            'organization': organization,
                            'vendor': getattr(source_analyzed_bill, 'vendor', None),
                            'bill_no': str(getattr(source_analyzed_bill, 'bill_no', ''))[:50],
                            'bill_date': getattr(source_analyzed_bill, 'bill_date', None),
                            'due_date': getattr(source_analyzed_bill, 'due_date', None),
                            'total': getattr(source_analyzed_bill, 'total', 0),
                            'igst': getattr(source_analyzed_bill, 'igst', 0),
                            'cgst': getattr(source_analyzed_bill, 'cgst', 0),
                            'sgst': getattr(source_analyzed_bill, 'sgst', 0),
                            'tds': getattr(source_analyzed_bill, 'tds', 0),
                            'note': (f"Moved from {from_module} - " + str(getattr(source_analyzed_bill, 'note', '')))[:100],
                            'consolidate': getattr(source_analyzed_bill, 'consolidate', False)
                        }

                        new_analyzed_bill = TallyExpenseAnalyzedBill.objects.create(**analyzed_common_data)

                # Transfer products/line items if they exist and bill was analysed
                if source_analyzed_bill and source_bill.status == 'Analysed' and new_analyzed_bill:
                    try:
                        # Transfer individual products
                        source_products = source_analyzed_bill.products.all() if hasattr(source_analyzed_bill, 'products') else []
                        for product in source_products:
                            if to_module == 'vendor':
                                TallyVendorAnalyzedProduct.objects.create(
                                    vendor_bill_analyzed=new_analyzed_bill,
                                    organization=organization,
                                    item_name=str(getattr(product, 'item_name', ''))[:100],
                                    item_details=str(getattr(product, 'item_details', '')),
                                    price=getattr(product, 'price', 0),
                                    quantity=getattr(product, 'quantity', 0),
                                    amount=getattr(product, 'amount', 0),
                                    product_gst=str(getattr(product, 'product_gst', ''))[:20],
                                    igst=getattr(product, 'igst', 0),
                                    cgst=getattr(product, 'cgst', 0),
                                    sgst=getattr(product, 'sgst', 0),
                                    stock_item=getattr(product, 'stock_item', None),
                                    taxes=getattr(product, 'taxes', None)
                                )
                            elif to_module == 'expense':
                                TallyExpenseAnalyzedProduct.objects.create(
                                    expense_bill=new_analyzed_bill,
                                    organization=organization,
                                    item_details=str(getattr(product, 'item_details', '') or getattr(product, 'item_name', '')),
                                    chart_of_accounts=getattr(product, 'ledger', None) or getattr(product, 'stock_item', None),
                                    amount=getattr(product, 'amount', 0),
                                    debit_or_credit=getattr(product, 'debit_credit', 'debit') or 'debit'
                                )

                        # Transfer consolidated products if they exist
                        source_consolidated = None
                        if hasattr(source_analyzed_bill, 'consolidated_product'):
                            try:
                                source_consolidated = source_analyzed_bill.consolidated_product
                            except (TallyVendorConsolidatedProduct.DoesNotExist, TallyExpenseConsolidatedProduct.DoesNotExist):
                                source_consolidated = None

                        if source_consolidated:
                            if to_module == 'vendor':
                                TallyVendorConsolidatedProduct.objects.create(
                                    vendor_bill_analyzed=new_analyzed_bill,
                                    organization=organization,
                                    item_name=str(getattr(source_consolidated, 'item_name', ''))[:500],
                                    item_details=str(getattr(source_consolidated, 'item_details', '')),
                                    price=getattr(source_consolidated, 'price', 0),
                                    quantity=getattr(source_consolidated, 'quantity', 1),
                                    amount=getattr(source_consolidated, 'amount', 0),
                                    product_gst=str(getattr(source_consolidated, 'product_gst', ''))[:20],
                                    igst=getattr(source_consolidated, 'igst', 0),
                                    cgst=getattr(source_consolidated, 'cgst', 0),
                                    sgst=getattr(source_consolidated, 'sgst', 0),
                                    stock_item=getattr(source_consolidated, 'stock_item', None),
                                    taxes=getattr(source_consolidated, 'taxes', None),
                                    original_items_count=getattr(source_consolidated, 'original_items_count', 1),
                                    consolidation_notes=f"Moved from {from_module} module"
                                )
                            elif to_module == 'expense':
                                TallyExpenseConsolidatedProduct.objects.create(
                                    expense_bill=new_analyzed_bill,
                                    organization=organization,
                                    item_details=str(getattr(source_consolidated, 'item_details', '') or getattr(source_consolidated, 'item_name', '')),
                                    chart_of_accounts=getattr(source_consolidated, 'chart_of_accounts', None) or getattr(source_consolidated, 'stock_item', None),
                                    amount=getattr(source_consolidated, 'amount', 0),
                                    debit_or_credit=getattr(source_consolidated, 'debit_or_credit', 'debit') or getattr(source_consolidated, 'debit_credit', 'debit') or 'debit',
                                    original_entries_count=getattr(source_consolidated, 'original_entries_count', 1) or getattr(source_consolidated, 'original_items_count', 1) or 1,
                                    consolidation_notes=f"Moved from {from_module} module"
                                )

                    except Exception as product_transfer_error:
                        logger.warning(f"Failed to transfer products for Tally bill {bill_id}: {product_transfer_error}")
                        # Don't fail the entire move operation, just log the warning

                # Delete source bill and related data
                if source_analyzed_bill:
                    # Delete products and consolidated products (cascade should handle this, but being explicit)
                    if hasattr(source_analyzed_bill, 'products'):
                        source_analyzed_bill.products.all().delete()
                    if hasattr(source_analyzed_bill, 'consolidated_product'):
                        try:
                            source_analyzed_bill.consolidated_product.delete()
                        except (TallyVendorConsolidatedProduct.DoesNotExist, TallyExpenseConsolidatedProduct.DoesNotExist):
                            pass
                    source_analyzed_bill.delete()

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
