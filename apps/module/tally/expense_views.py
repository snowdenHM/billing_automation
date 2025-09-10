import os
import json
import base64
import logging
from datetime import datetime
from io import BytesIO
from PyPDF2 import PdfReader
from pdf2image import convert_from_bytes

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework_api_key.permissions import HasAPIKey
from rest_framework.parsers import MultiPartParser, FormParser
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.core.files.base import ContentFile
from django.conf import settings
from drf_spectacular.utils import extend_schema, OpenApiResponse

from apps.organizations.models import Organization

from apps.module.tally.models import (
    TallyExpenseBill, TallyExpenseAnalyzedBill, TallyExpenseAnalyzedProduct,
    Ledger, ParentLedger
)
from apps.module.tally.serializers import (
    TallyExpenseBillSerializer,
    TallyExpenseAnalyzedBillSerializer,
    TallyExpenseAnalyzedProductSerializer,
    ExpenseBillUploadSerializer,
    ExpenseBillAnalysisRequestSerializer,
    ExpenseBillVerificationSerializer,
    ExpenseBillSyncRequestSerializer,
    ExpenseBillSyncResponseSerializer
)
from apps.common.permissions import IsOrgAdmin

# OpenAI Client
try:
    from openai import OpenAI
    client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))
except ImportError:
    client = None

logger = logging.getLogger(__name__)


class OrganizationAPIKeyOrBearerToken(BasePermission):
    """
    Custom permission class that allows access via API key OR Bearer token authentication.
    This is an OR condition between authentication methods.
    """

    def has_permission(self, request, view):
        # Check for API key in the Authorization header
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')

        if auth_header.startswith('Api-Key '):
            api_key_value = auth_header.replace('Api-Key ', '', 1)

            # Check if the API key exists and is valid
            from rest_framework_api_key.models import APIKey
            from apps.organizations.models import OrganizationAPIKey

            try:
                # First check if the raw API key is valid
                api_key_obj = APIKey.objects.get_from_key(api_key_value)

                # Check if the API key is valid with the actual key string
                if api_key_obj and api_key_obj.is_valid(api_key_value):
                    # Then check if it's linked to an organization
                    org_api_key = OrganizationAPIKey.objects.get(api_key=api_key_obj)

                    # Store the organization in the request for later use
                    request.organization = org_api_key.organization
                    return True
            except Exception:
                # Any exception means the API key is invalid or doesn't exist
                pass

        # If not authenticated via API key, check for Bearer token
        bearer_auth = IsAuthenticated().has_permission(request, view)
        if bearer_auth:
            # If authenticated via bearer token, also check admin permission
            return IsOrgAdmin().has_permission(request, view)

        return False


@extend_schema(tags=['Tally Expense Bills'])
class TallyExpenseBillViewSet(viewsets.ModelViewSet):
    """
    ViewSet for handling Tally Expense Bills with complete workflow:
    Draft → Analyzed → Verified → Synced
    """
    serializer_class = TallyExpenseBillSerializer
    permission_classes = [OrganizationAPIKeyOrBearerToken]
    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self):
        """Filter queryset based on organization"""
        organization = self.get_organization()
        return TallyExpenseBill.objects.filter(organization=organization).order_by('-created_at')

    def get_organization(self):
        """Get organization from URL UUID parameter or API key"""
        org_id = self.kwargs.get('org_id')
        if org_id:
            return get_object_or_404(Organization, id=org_id)

        if hasattr(self.request, 'auth') and self.request.auth:
            from apps.organizations.models import OrganizationAPIKey
            try:
                org_api_key = OrganizationAPIKey.objects.get(api_key=self.request.auth)
                return org_api_key.organization
            except OrganizationAPIKey.DoesNotExist:
                pass

        if hasattr(self.request.user, 'memberships'):
            membership = self.request.user.memberships.first()
            if membership:
                return membership.organization
        return None

    def perform_create(self, serializer):
        """Set organization when creating expense bill"""
        organization = self.get_organization()
        serializer.save(organization=organization)

    @extend_schema(
        summary="List Expense Bills",
        description="Get all expense bills for the organization",
        responses={200: TallyExpenseBillSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        summary="Upload Expense Bills",
        description="Upload single or multiple expense bill files (PDF, JPG, PNG)",
        request=ExpenseBillUploadSerializer,
        responses={201: TallyExpenseBillSerializer(many=True)},
    )
    @action(detail=False, methods=['post'])
    def upload(self, request, *args, **kwargs):
        """Handle expense bill file uploads with PDF splitting support"""
        serializer = ExpenseBillUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        organization = self.get_organization()
        if not organization:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_400_BAD_REQUEST
            )

        files = serializer.validated_data['files']
        file_type = serializer.validated_data['file_type']
        created_bills = []

        try:
            with transaction.atomic():
                for uploaded_file in files:
                    file_extension = uploaded_file.name.lower().split('.')[-1]

                    # Handle PDF splitting for multiple invoice files
                    if (file_type == TallyExpenseBill.BillType.MULTI and
                        file_extension == 'pdf'):

                        pdf_bills = self._process_pdf_splitting(
                            uploaded_file, organization, file_type
                        )
                        created_bills.extend(pdf_bills)
                    else:
                        # Create single bill for non-PDF or single invoice type
                        bill = TallyExpenseBill.objects.create(
                            file=uploaded_file,
                            file_type=file_type,
                            organization=organization
                        )
                        created_bills.append(bill)

            response_serializer = TallyExpenseBillSerializer(created_bills, many=True)
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"Error uploading expense bills: {str(e)}")
            return Response(
                {'error': f'Error processing files: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )

    def _process_pdf_splitting(self, pdf_file, organization, file_type):
        """Split PDF into individual pages and create separate bills"""
        created_bills = []

        try:
            pdf_file.seek(0)
            pdf_bytes = pdf_file.read()
            pdf = PdfReader(BytesIO(pdf_bytes))
            unique_id = datetime.now().strftime("%Y%m%d%H%M%S")

            for page_num in range(len(pdf.pages)):
                # Convert PDF page to image
                page_images = convert_from_bytes(
                    pdf_bytes,
                    first_page=page_num + 1,
                    last_page=page_num + 1
                )

                if page_images:
                    image_io = BytesIO()
                    page_images[0].save(image_io, format='JPEG')
                    image_io.seek(0)

                    # Create bill for this page
                    bill = TallyExpenseBill.objects.create(
                        file=ContentFile(
                            image_io.read(),
                            name=f"BM-Expense-Page-{page_num + 1}-{unique_id}.jpg"
                        ),
                        file_type=file_type,
                        organization=organization
                    )
                    created_bills.append(bill)

        except Exception as e:
            logger.error(f"Error splitting PDF: {str(e)}")
            raise Exception(f"PDF processing failed: {str(e)}")

        return created_bills

    @extend_schema(
        summary="Analyze Expense Bill",
        description="Analyze expense bill using OpenAI to extract expense data",
        request=ExpenseBillAnalysisRequestSerializer,
        responses={
            200: TallyExpenseAnalyzedBillSerializer,
            400: OpenApiResponse(description="Analysis failed")
        }
    )
    @action(detail=False, methods=['post'])
    def analyze(self, request, *args, **kwargs):
        """Analyze expense bill using OpenAI"""
        serializer = ExpenseBillAnalysisRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        bill_id = serializer.validated_data['bill_id']
        organization = self.get_organization()

        try:
            bill = TallyExpenseBill.objects.get(
                id=bill_id,
                organization=organization
            )
        except TallyExpenseBill.DoesNotExist:
            return Response(
                {'error': 'Bill not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        if bill.status != TallyExpenseBill.BillStatus.DRAFT:
            return Response(
                {'error': 'Bill is not in draft status'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            analyzed_bill = self._analyze_bill_with_ai(bill, organization)
            serializer = TallyExpenseAnalyzedBillSerializer(analyzed_bill)
            return Response(serializer.data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Expense bill analysis failed: {str(e)}")
            return Response(
                {'error': f'Analysis failed: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )

    def _analyze_bill_with_ai(self, bill, organization):
        """Analyze expense bill using OpenAI API"""
        if not client:
            raise Exception("OpenAI client not configured")

        # Read file and convert to base64
        try:
            with open(bill.file.path, 'rb') as f:
                image_base64 = base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            raise Exception(f"Error reading bill file: {str(e)}")

        # Expense schema for AI extraction
        expense_schema = {
            "$schema": "http://json-schema.org/draft/2020-12/schema",
            "title": "Expense Bill",
            "description": "An expense bill format",
            "type": "object",
            "properties": {
                "billNumber": {"type": "string"},
                "dateIssued": {"type": "string", "format": "date"},
                "from": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "address": {"type": "string"}
                    }
                },
                "to": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "address": {"type": "string"}
                    }
                },
                "expenses": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "category": {"type": "string"},
                            "amount": {"type": "number"}
                        }
                    }
                },
                "total": {"type": "number"},
                "igst": {"type": "number"},
                "cgst": {"type": "number"},
                "sgst": {"type": "number"}
            }
        }

        # AI processing request
        try:
            response = client.chat.completions.create(
                model='gpt-4o',
                response_format={"type": "json_object"},
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Extract expense bill data in JSON format using this schema: {json.dumps(expense_schema)}"
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                        }
                    ]
                }],
                max_tokens=1000
            )
            json_data = json.loads(response.choices[0].message.content)
        except Exception as e:
            raise Exception(f"AI processing failed: {str(e)}")

        # Process and save extracted data
        return self._process_analysis_data(bill, json_data, organization)

    def _process_analysis_data(self, bill, json_data, organization):
        """Process AI extracted data and create analyzed expense bill"""
        try:
            # Extract relevant data
            if "properties" in json_data:
                # Handle schema format
                relevant_data = {
                    "billNumber": json_data["properties"]["billNumber"]["const"],
                    "dateIssued": json_data["properties"]["dateIssued"]["const"],
                    "from": json_data["properties"]["from"]["properties"],
                    "to": json_data["properties"]["to"]["properties"],
                    "expenses": [
                        {
                            "description": item["description"]["const"],
                            "category": item["category"]["const"],
                            "amount": item["amount"]["const"]
                        }
                        for item in json_data["properties"]["expenses"]["items"]
                    ],
                    "total": json_data["properties"]["total"]["const"],
                    "igst": json_data["properties"]["igst"]["const"],
                    "cgst": json_data["properties"]["cgst"]["const"],
                    "sgst": json_data["properties"]["sgst"]["const"],
                }
            else:
                relevant_data = json_data

            # Save analyzed data to bill
            bill.analysed_data = relevant_data
            bill.save(update_fields=['analysed_data'])

            # Extract required fields
            bill_number = relevant_data.get('billNumber', '').strip()
            date_issued = relevant_data.get('dateIssued', '')
            company_name = relevant_data.get('from', {}).get('name', '').strip().lower()

            # Parse date
            bill_date = None
            if date_issued:
                try:
                    bill_date = datetime.strptime(date_issued, '%Y-%m-%d').date()
                except ValueError:
                    pass

            # Find vendor ledger (same logic as vendor bills)
            vendor = self._find_vendor_ledger(company_name, organization)

            # Create analyzed bill
            with transaction.atomic():
                analyzed_bill = TallyExpenseAnalyzedBill.objects.create(
                    selected_bill=bill,
                    vendor=vendor,
                    bill_no=bill_number,
                    bill_date=bill_date,
                    igst=float(relevant_data.get('igst') or 0),
                    cgst=float(relevant_data.get('cgst') or 0),
                    sgst=float(relevant_data.get('sgt') or 0),
                    total=relevant_data.get('total', 0),
                    note="AI Analyzed Expense Bill",
                    organization=organization
                )

                # Create analyzed products (expense items)
                product_instances = []
                for expense in relevant_data.get('expenses', []):
                    product = TallyExpenseAnalyzedProduct(
                        expense_bill=analyzed_bill,
                        item_details=expense.get('description', ''),
                        amount=float(expense.get('amount', 0) or 0),
                        debit_or_credit=TallyExpenseAnalyzedProduct.DebitCredit.DEBIT,  # Expenses are typically debits
                        organization=organization
                    )
                    product_instances.append(product)

                TallyExpenseAnalyzedProduct.objects.bulk_create(product_instances)

                # Update bill status
                bill.status = TallyExpenseBill.BillStatus.ANALYSED
                bill.process = True
                bill.save(update_fields=['status', 'process'])

                return analyzed_bill

        except Exception as e:
            raise Exception(f"Error processing analysis data: {str(e)}")

    def _find_vendor_ledger(self, company_name, organization):
        """Find matching vendor ledger"""
        try:
            parent_ledger = ParentLedger.objects.get(
                parent="Sundry Creditors",
                organization=organization
            )
            vendor_list = Ledger.objects.filter(
                parent=parent_ledger,
                organization=organization
            )

            # Find matching vendor (case-insensitive exact match first)
            vendor = vendor_list.filter(name__iexact=company_name).first()
            if not vendor:
                vendor = vendor_list.filter(name__icontains=company_name).first()

            return vendor

        except ParentLedger.DoesNotExist:
            return None

    @extend_schema(
        summary="Verify Expense Bill",
        description="Verify analyzed expense bill data and mark as verified",
        request=ExpenseBillVerificationSerializer,
        responses={200: TallyExpenseAnalyzedBillSerializer}
    )
    @action(detail=False, methods=['post'])
    def verify(self, request, *args, **kwargs):
        """Verify analyzed expense bill"""
        serializer = ExpenseBillVerificationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        bill_id = request.data.get('bill_id')
        organization = self.get_organization()

        try:
            bill = TallyExpenseBill.objects.get(id=bill_id, organization=organization)
            analyzed_bill = TallyExpenseAnalyzedBill.objects.get(selected_bill=bill)
        except (TallyExpenseBill.DoesNotExist, TallyExpenseAnalyzedBill.DoesNotExist):
            return Response(
                {'error': 'Bill or analyzed data not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        if bill.status != TallyExpenseBill.BillStatus.ANALYSED:
            return Response(
                {'error': 'Bill is not in analyzed status'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            verified_bill = self._verify_bill_data(analyzed_bill, serializer.validated_data, organization)

            # Update bill status
            bill.status = TallyExpenseBill.BillStatus.VERIFIED
            bill.save(update_fields=['status'])

            response_serializer = TallyExpenseAnalyzedBillSerializer(verified_bill)
            return Response(response_serializer.data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Expense bill verification failed: {str(e)}")
            return Response(
                {'error': f'Verification failed: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )

    def _verify_bill_data(self, analyzed_bill, verification_data, organization):
        """Verify and update expense bill data"""
        with transaction.atomic():
            # Update bill fields
            if 'vendor_id' in verification_data and verification_data['vendor_id']:
                try:
                    analyzed_bill.vendor = Ledger.objects.get(
                        id=verification_data['vendor_id'],
                        organization=organization
                    )
                except Ledger.DoesNotExist:
                    pass

            # Update basic fields
            for field in ['voucher', 'bill_no', 'bill_date', 'note', 'igst', 'cgst', 'sgt']:
                if field in verification_data:
                    setattr(analyzed_bill, field, verification_data[field])

            # Update tax ledgers
            tax_fields = ['igst_taxes_id', 'cgst_taxes_id', 'sgst_taxes_id']
            for tax_field in tax_fields:
                if tax_field in verification_data and verification_data[tax_field]:
                    ledger_field = tax_field.replace('_id', '')
                    try:
                        tax_ledger = Ledger.objects.get(
                            id=verification_data[tax_field],
                            organization=organization
                        )
                        setattr(analyzed_bill, ledger_field, tax_ledger)
                    except Ledger.DoesNotExist:
                        pass

            analyzed_bill.save()

            # Update products if provided
            if 'products' in verification_data:
                self._update_expense_products(analyzed_bill, verification_data['products'], organization)

            return analyzed_bill

    def _update_expense_products(self, analyzed_bill, products_data, organization):
        """Update analyzed expense products"""
        for product_data in products_data:
            try:
                product = TallyExpenseAnalyzedProduct.objects.get(
                    id=product_data['id'],
                    expense_bill=analyzed_bill
                )

                # Update product fields
                for field in ['item_details', 'amount', 'debit_or_credit']:
                    if field in product_data:
                        setattr(product, field, product_data[field])

                # Update chart of accounts ledger
                if 'chart_of_accounts_id' in product_data and product_data['chart_of_accounts_id']:
                    try:
                        product.chart_of_accounts = Ledger.objects.get(
                            id=product_data['chart_of_accounts_id'],
                            organization=organization
                        )
                    except Ledger.DoesNotExist:
                        pass

                product.save()

            except TallyExpenseAnalyzedProduct.DoesNotExist:
                continue

    @extend_schema(
        summary="Get Bills by Status",
        description="Get expense bills filtered by status",
        responses={200: TallyExpenseBillSerializer(many=True)},
    )
    @action(detail=False, methods=['get'])
    def by_status(self, request, *args, **kwargs):
        """Get bills filtered by status"""
        status_filter = request.query_params.get('status')

        if not status_filter:
            return Response(
                {'error': 'Status parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        queryset = self.get_queryset().filter(status=status_filter)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="Get All Synced Expense Bills",
        description="Get all synced expense bills with their products for the organization",
        responses={200: ExpenseBillSyncResponseSerializer(many=True)},
        tags=['Tally TCP']
    )
    @action(detail=False, methods=['get'])
    def sync_bills(self, request, *args, **kwargs):
        """Get all synced expense bills with their products"""
        organization = self.get_organization()

        if not organization:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get all analyzed bills where the main bill status is "Synced"
        analyzed_bills = TallyExpenseAnalyzedBill.objects.filter(
            organization=organization,
            selected_bill__status=TallyExpenseBill.BillStatus.SYNCED
        ).select_related(
            'selected_bill', 'vendor', 'igst_taxes', 'cgst_taxes', 'sgst_taxes'
        ).prefetch_related(
            'products__chart_of_accounts'
        ).order_by('-created_at')

        # Convert each analyzed bill to the new sync format
        sync_data_list = []
        for analyzed_bill in analyzed_bills:
            sync_data = self._prepare_expense_sync_data(analyzed_bill, organization)
            sync_data_list.append(sync_data)

        return Response(sync_data_list, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Sync Expense Bill to External System",
        description="Send expense bill data to external system via POST request",
        request=ExpenseBillSyncRequestSerializer,
        responses={
            200: OpenApiResponse(description="Bill synced successfully"),
            400: OpenApiResponse(description="Sync failed")
        },
        tags=['Tally TCP']
    )
    @action(detail=False, methods=['post'])
    def sync_external(self, request, *args, **kwargs):
        """Sync expense bill to external system"""
        serializer = ExpenseBillSyncRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        bill_id = serializer.validated_data['bill_id']
        organization = self.get_organization()

        try:
            bill = TallyExpenseBill.objects.get(id=bill_id, organization=organization)
            analyzed_bill = TallyExpenseAnalyzedBill.objects.get(selected_bill=bill)
        except (TallyExpenseBill.DoesNotExist, TallyExpenseAnalyzedBill.DoesNotExist):
            return Response(
                {'error': 'Bill or analyzed data not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        if bill.status != TallyExpenseBill.BillStatus.VERIFIED:
            return Response(
                {'error': 'Bill must be verified before syncing'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Prepare the sync payload
            sync_payload = self._prepare_expense_sync_data(analyzed_bill, organization)

            # Log the sync attempt
            logger.info(f"Syncing expense bill {bill_id} for organization {organization.id}")

            # Update bill status to synced
            bill.status = TallyExpenseBill.BillStatus.SYNCED
            bill.save(update_fields=['status'])

            # Return success response with the payload
            return Response({
                'message': 'Expense bill synced successfully',
                'bill_id': str(bill_id),
                'status': 'Synced',
                'sync_data': sync_payload
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Expense bill sync failed for bill {bill_id}: {str(e)}")
            return Response(
                {'error': f'Sync failed: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )

    def _prepare_expense_sync_data(self, analyzed_bill, organization):
        """Prepare expense bill data for sync"""
        vendor_ledger = analyzed_bill.vendor

        # Convert bill date to dd-mm-yyyy format
        bill_date_str = (analyzed_bill.bill_date.strftime('%d-%m-%Y')
                        if analyzed_bill.bill_date else None)

        # Initialize DR and CR ledgers
        dr_ledger = []
        cr_ledger = []

        # Process expense line items based ONLY on their debit_or_credit field
        total_debit = 0
        total_credit = 0

        for item in analyzed_bill.products.all():
            if item.amount and item.amount > 0:
                ledger_entry = {
                    "LEDGERNAME": str(item.chart_of_accounts) if item.chart_of_accounts else "No COA Ledger",
                    "AMOUNT": float(item.amount)
                }
                
                # Simple rule: debit goes to DR_LEDGER, credit goes to CR_LEDGER
                if item.debit_or_credit == 'debit':
                    dr_ledger.append(ledger_entry)
                    total_debit += float(item.amount)
                elif item.debit_or_credit == 'credit':
                    cr_ledger.append(ledger_entry)
                    total_credit += float(item.amount)

        # Ensure debit and credit are balanced
        # If only debits exist, add vendor as credit
        if total_debit > 0 and total_credit == 0:
            cr_ledger.append({
                "LEDGERNAME": vendor_ledger.name if vendor_ledger else "No Vendor Ledger",
                "AMOUNT": total_debit
            })
        # If only credits exist, add vendor as debit
        elif total_credit > 0 and total_debit == 0:
            dr_ledger.append({
                "LEDGERNAME": vendor_ledger.name if vendor_ledger else "No Vendor Ledger",
                "AMOUNT": total_credit
            })

        # Build sync payload
        sync_data = {
            "id": str(analyzed_bill.selected_bill.id),
            "voucher": analyzed_bill.voucher or "",
            "bill_no": analyzed_bill.bill_no or "",
            "bill_date": bill_date_str,
            "total": float(analyzed_bill.total or 0),
            "name": vendor_ledger.name if vendor_ledger else "No Ledger",
            "company": vendor_ledger.company if vendor_ledger else "No Ledger",
            "gst_in": vendor_ledger.gst_in if vendor_ledger else "0",
            "DR_LEDGER": dr_ledger,
            "CR_LEDGER": cr_ledger,
            "note": analyzed_bill.note or "AI Analyzed Expense Bill",
        }

        return {
            "data": sync_data
        }
