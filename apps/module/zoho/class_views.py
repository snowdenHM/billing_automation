# apps/module/zoho/class_views.py

from django.db import transaction
from django.utils import timezone

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser

from drf_spectacular.utils import extend_schema, OpenApiResponse

from .models import (
    ZohoCredentials,
    ZohoVendor,
    ZohoChartOfAccount,
    ZohoTaxes,
    ZohoTdsTcs,
    VendorBill,
    VendorZohoBill,
    ExpenseBill,
    ExpenseZohoBill,
)
from .serializers.settings import (
    ZohoCredentialsSerializer,
    ZohoVendorSerializer,
    ZohoChartOfAccountSerializer,
    ZohoTaxesSerializer,
    ZohoTdsTcsSerializer,
)
from .serializers.vendor_bills import (
    ZohoVendorBillSerializer,
    ZohoSyncResultSerializer,
    ZohoAnalysisResultSerializer,
    ZohoOperationResultSerializer,
)
from .serializers.expense_bills import (
    ZohoExpenseBillSerializer,
)
from .serializers.common import (
    TokenResponseSerializer,
    SyncResponseSerializer,
    AnalysisResponseSerializer,
    ZohoSyncResponseSerializer,
)
from .views import (
    get_organization_from_request,
    get_zoho_credentials,
    make_zoho_api_request,
    analyze_bill_with_openai,
)


# ============================================================================
# Zoho Ops/Settings Views
# ============================================================================

class GenerateTokenView(GenericAPIView):
    """Generate access and refresh tokens using the access code from Zoho OAuth."""
    permission_classes = [IsAuthenticated]
    serializer_class = TokenResponseSerializer

    @extend_schema(
        responses={200: TokenResponseSerializer},
        tags=["Zoho Ops"],
    )
    def post(self, request):
        organization = get_organization_from_request(request)
        if not organization:
            return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            credentials = ZohoCredentials.objects.get(organization=organization)
        except ZohoCredentials.DoesNotExist:
            return Response(
                {"detail": "Zoho credentials not found. Please configure credentials first."},
                status=status.HTTP_404_NOT_FOUND
            )

        if not credentials.accessCode or credentials.accessCode == "Your Access Code":
            return Response(
                {"detail": "Access code not provided. Please set the access code first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Prepare token generation request
        token_url = "https://accounts.zoho.in/oauth/v2/token"

        token_data = {
            'code': credentials.accessCode,
            'client_id': credentials.clientId,
            'client_secret': credentials.clientSecret,
            'redirect_uri': credentials.redirectUrl,
            'grant_type': 'authorization_code'
        }

        try:
            # Make request to Zoho OAuth API
            import requests
            response = requests.post(token_url, data=token_data, timeout=30)

            if response.status_code == 200:
                token_response = response.json()

                # Update credentials with new tokens
                credentials.accessToken = token_response.get('access_token')
                credentials.refreshToken = token_response.get('refresh_token')

                # Set token expiry (Zoho tokens typically last 1 hour)
                expires_in = token_response.get('expires_in', 3600)
                credentials.token_expiry = timezone.now() + timezone.timedelta(seconds=expires_in)

                credentials.save(update_fields=['accessToken', 'refreshToken', 'token_expiry', 'update_at'])

                return Response({
                    "detail": "Tokens generated successfully",
                    "access_token": credentials.accessToken,
                    "refresh_token": credentials.refreshToken,
                    "expires_in": expires_in,
                    "token_expiry": credentials.token_expiry
                })

            else:
                error_data = response.json() if response.content else {}
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Zoho token generation failed: {response.status_code} - {response.text}")

                return Response({
                    "detail": f"Token generation failed: {error_data.get('error_description', 'Unknown error')}",
                    "error_code": error_data.get('error', 'token_generation_failed')
                }, status=status.HTTP_400_BAD_REQUEST)

        except requests.RequestException as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Network error during token generation: {str(e)}")
            return Response(
                {"detail": f"Network error: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Unexpected error during token generation: {str(e)}")
            return Response(
                {"detail": f"Unexpected error: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# ============================================================================
# Zoho Sync Views
# ============================================================================

class VendorsSyncView(GenericAPIView):
    """Sync vendors from Zoho Books."""
    permission_classes = [IsAuthenticated]
    serializer_class = SyncResponseSerializer

    @extend_schema(
        responses={200: SyncResponseSerializer},
        tags=["Zoho Ops"],
    )
    def post(self, request, org_id):
        organization = get_organization_from_request(request, org_id=org_id)
        if not organization:
            return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            credentials = get_zoho_credentials(organization)
            zoho_data = make_zoho_api_request(credentials, "contacts")

            synced_count = 0
            for contact in zoho_data.get('contacts', []):
                if contact.get('contact_type') == 'vendor':
                    vendor, created = ZohoVendor.objects.update_or_create(
                        organization=organization,
                        contactId=contact['contact_id'],
                        defaults={
                            'companyName': contact.get('company_name', ''),
                            'gstNo': contact.get('gst_no', '')
                        }
                    )
                    if created:
                        synced_count += 1

            return Response({
                "detail": f"Successfully synced {synced_count} vendors",
                "synced_count": synced_count
            })
        except Exception as e:
            return Response(
                {"detail": f"Sync failed: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ChartOfAccountsSyncView(GenericAPIView):
    """Sync chart of accounts from Zoho Books."""
    permission_classes = [IsAuthenticated]
    serializer_class = SyncResponseSerializer

    @extend_schema(
        responses={200: SyncResponseSerializer},
        tags=["Zoho Ops"],
    )
    def post(self, request, org_id):
        organization = get_organization_from_request(request, org_id=org_id)
        if not organization:
            return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            credentials = get_zoho_credentials(organization)
            zoho_data = make_zoho_api_request(credentials, "chartofaccounts")

            synced_count = 0
            for account in zoho_data.get('chartofaccounts', []):
                chart_account, created = ZohoChartOfAccount.objects.update_or_create(
                    organization=organization,
                    accountId=account['account_id'],
                    defaults={
                        'accountName': account.get('account_name', '')
                    }
                )
                if created:
                    synced_count += 1

            return Response({
                "detail": f"Successfully synced {synced_count} chart of accounts",
                "synced_count": synced_count
            })
        except Exception as e:
            return Response(
                {"detail": f"Sync failed: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class TaxesSyncView(GenericAPIView):
    """Sync taxes from Zoho Books."""
    permission_classes = [IsAuthenticated]
    serializer_class = SyncResponseSerializer

    @extend_schema(
        responses={200: SyncResponseSerializer},
        tags=["Zoho Ops"],
    )
    def post(self, request, org_id):
        organization = get_organization_from_request(request, org_id=org_id)
        if not organization:
            return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            credentials = get_zoho_credentials(organization)
            zoho_data = make_zoho_api_request(credentials, "settings/taxes")

            synced_count = 0
            for tax in zoho_data.get('taxes', []):
                tax_obj, created = ZohoTaxes.objects.update_or_create(
                    organization=organization,
                    taxId=tax['tax_id'],
                    defaults={
                        'taxName': tax.get('tax_name', '')
                    }
                )
                if created:
                    synced_count += 1

            return Response({
                "detail": f"Successfully synced {synced_count} taxes",
                "synced_count": synced_count
            })
        except Exception as e:
            return Response(
                {"detail": f"Sync failed: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class TdsTcsSyncView(GenericAPIView):
    """Sync TDS/TCS from Zoho Books."""
    permission_classes = [IsAuthenticated]
    serializer_class = SyncResponseSerializer

    @extend_schema(
        responses={200: SyncResponseSerializer},
        tags=["Zoho Ops"],
    )
    def post(self, request, org_id):
        organization = get_organization_from_request(request, org_id=org_id)
        if not organization:
            return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            credentials = get_zoho_credentials(organization)
            import requests

            # Sync TDS taxes - using direct URL approach to match the specified format
            tds_url = f"https://www.zohoapis.in/books/v3/settings/taxes?is_tds_request=true&organization_id={credentials.organisationId}"
            headers = {
                'Authorization': f'Zoho-oauthtoken {credentials.accessToken}',
                'Content-Type': 'application/json'
            }

            tds_response = requests.get(tds_url, headers=headers)
            tds_response.raise_for_status()
            tds_data = tds_response.json()

            tds_count = 0
            for tax in tds_data.get('taxes', []):
                tds_obj, created = ZohoTdsTcs.objects.update_or_create(
                    organization=organization,
                    taxId=tax['tax_id'],
                    defaults={
                        'taxName': tax.get('tax_name', ''),
                        'taxPercentage': str(tax.get('tax_percentage', 0)),
                        'taxType': 'TDS'
                    }
                )
                if created:
                    tds_count += 1

            # Sync TCS taxes - using direct URL approach to match the specified format
            tcs_url = f"https://www.zohoapis.in/books/v3/settings/taxes?is_tcs_request=true&filter_by=Taxes.All&organization_id={credentials.organisationId}"

            tcs_response = requests.get(tcs_url, headers=headers)
            tcs_response.raise_for_status()
            tcs_data = tcs_response.json()

            tcs_count = 0
            for tax in tcs_data.get('taxes', []):
                tcs_obj, created = ZohoTdsTcs.objects.update_or_create(
                    organization=organization,
                    taxId=tax['tax_id'],
                    defaults={
                        'taxName': tax.get('tax_name', ''),
                        'taxPercentage': str(tax.get('tax_percentage', 0)),
                        'taxType': 'TCS'
                    }
                )
                if created:
                    tcs_count += 1

            total_synced = tds_count + tcs_count
            return Response({
                "detail": f"Successfully synced {total_synced} TDS/TCS ({tds_count} TDS, {tcs_count} TCS)",
                "synced_count": total_synced
            })
        except requests.RequestException as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Zoho API request failed: {str(e)}")
            return Response(
                {"detail": f"Sync failed: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        except Exception as e:
            return Response(
                {"detail": f"Sync failed: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# ============================================================================
# Vendor Bill Views
# ============================================================================

class VendorBillAnalyzeView(GenericAPIView):
    """Analyze vendor bill using OpenAI. Changes status from 'Draft' to 'Analyzed'."""
    permission_classes = [IsAuthenticated]
    serializer_class = AnalysisResponseSerializer

    @extend_schema(
        responses={200: AnalysisResponseSerializer},
        tags=["Zoho Vendor Bills"],
    )
    def post(self, request, org_id, bill_id):
        organization = get_organization_from_request(request, org_id=org_id)
        if not organization:
            return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            bill = VendorBill.objects.get(id=bill_id, organization=organization)

            if bill.status != 'Draft':
                return Response(
                    {"detail": "Bill must be in 'Draft' status to analyze"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Read file content
            bill.file.seek(0)
            file_content = bill.file.read()
            file_extension = bill.file.name.split('.')[-1].lower()

            # Analyze with OpenAI
            analyzed_data = analyze_bill_with_openai(file_content, file_extension)

            # Update bill with analyzed data and status
            with transaction.atomic():
                bill.analysed_data = analyzed_data
                bill.status = 'Analysed'
                bill.process = True
                bill.save()

                # Create VendorZohoBill and VendorZohoProduct objects
                self._create_zoho_objects(bill, analyzed_data, organization)

            return Response({
                "detail": "Bill analyzed successfully",
                "analyzed_data": analyzed_data
            })

        except VendorBill.DoesNotExist:
            return Response({"detail": "Vendor bill not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Analysis failed: {str(e)}")
            return Response(
                {"detail": f"Analysis failed: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _create_zoho_objects(self, bill, analyzed_data, organization):
        """
        Create VendorZohoBill and VendorZohoProduct objects from analyzed data.
        """
        from datetime import datetime
        from decimal import Decimal
        import logging

        logger = logging.getLogger(__name__)
        logger.info(f"Creating Zoho objects for bill {bill.id} with analyzed data")

        # Extract required fields safely
        invoice_number = analyzed_data.get('invoiceNumber', '').strip()
        date_issued = analyzed_data.get('dateIssued', '')
        vendor_name = analyzed_data.get('vendorName', '').strip()
        vendor_gst = analyzed_data.get('vendorGST', '').strip()

        # Parse date with multiple format support
        bill_date = None
        if date_issued:
            date_formats = ['%Y-%m-%d', '%d-%m-%Y', '%m/%d/%Y', '%d/%m/%Y']
            for date_format in date_formats:
                try:
                    bill_date = datetime.strptime(date_issued, date_format).date()
                    break
                except (ValueError, TypeError):
                    continue

            if not bill_date:
                logger.warning(f"Could not parse date: {date_issued}")

        # Find vendor by GST number first, then by name (case-insensitive search)
        vendor = None
        if vendor_gst:
            vendor = ZohoVendor.objects.filter(
                organization=organization,
                gstNo__iexact=vendor_gst
            ).first()
            logger.info(f"Found vendor by GST {vendor_gst}: {vendor}")

        if not vendor and vendor_name:
            vendor = ZohoVendor.objects.filter(
                organization=organization,
                companyName__icontains=vendor_name
            ).first()
            logger.info(f"Found vendor by name {vendor_name}: {vendor}")

        # Validate and convert numeric fields
        def safe_decimal_str(value, default='0'):
            try:
                if value is None:
                    return default
                # Handle both int/float and string inputs
                if isinstance(value, (int, float)):
                    return str(value)
                # If it's already a string, validate it's numeric
                float(str(value))  # This will raise ValueError if not numeric
                return str(value)
            except (ValueError, TypeError):
                logger.warning(f"Invalid numeric value: {value}, using default: {default}")
                return default

        # Create or update VendorZohoBill
        try:
            zoho_bill, created = VendorZohoBill.objects.get_or_create(
                selectBill=bill,
                organization=organization,
                defaults={
                    'vendor': vendor,
                    'bill_no': invoice_number,
                    'bill_date': bill_date,
                    'total': safe_decimal_str(analyzed_data.get('total')),
                    'igst': safe_decimal_str(analyzed_data.get('igst')),
                    'cgst': safe_decimal_str(analyzed_data.get('cgst')),
                    'sgst': safe_decimal_str(analyzed_data.get('sgst')),
                    'note': f"AI Analyzed Bill - {vendor_name or 'Unknown Vendor'}"
                }
            )

            if created:
                logger.info(f"Created new VendorZohoBill: {zoho_bill.id}")
            else:
                logger.info(f"Found existing VendorZohoBill: {zoho_bill.id}")
                # Update the existing bill with new analyzed data
                zoho_bill.vendor = vendor
                zoho_bill.bill_no = invoice_number or zoho_bill.bill_no
                zoho_bill.bill_date = bill_date or zoho_bill.bill_date
                zoho_bill.total = safe_decimal_str(analyzed_data.get('total'), zoho_bill.total)
                zoho_bill.igst = safe_decimal_str(analyzed_data.get('igst'), zoho_bill.igst)
                zoho_bill.cgst = safe_decimal_str(analyzed_data.get('cgst'), zoho_bill.cgst)
                zoho_bill.sgst = safe_decimal_str(analyzed_data.get('sgst'), zoho_bill.sgst)
                zoho_bill.note = f"AI Analyzed Bill - {vendor_name or 'Unknown Vendor'}"
                zoho_bill.save()
                logger.info(f"Updated existing VendorZohoBill: {zoho_bill.id}")

            # Delete existing products and recreate them
            existing_products_count = zoho_bill.products.count()
            if existing_products_count > 0:
                zoho_bill.products.all().delete()
                logger.info(f"Deleted {existing_products_count} existing products")

            # Create VendorZohoProduct objects for each item
            items = analyzed_data.get('items', [])
            logger.info(f"Creating {len(items)} product line items")

            from .models import VendorZohoProduct

            created_products = []
            for idx, item in enumerate(items):
                try:
                    # Calculate amount if not provided
                    rate = item.get('rate', 0) or 0
                    quantity = item.get('quantity', 1) or 1
                    amount = item.get('amount')

                    if amount is None:
                        amount = rate * quantity

                    product = VendorZohoProduct.objects.create(
                        zohoBill=zoho_bill,
                        organization=organization,
                        item_name=item.get('description', f'Item {idx + 1}')[:100],  # Truncate to field limit
                        item_details=item.get('description', f'Item {idx + 1}')[:200],  # Truncate to field limit
                        rate=safe_decimal_str(rate),
                        quantity=safe_decimal_str(quantity, '1'),
                        amount=safe_decimal_str(amount)
                    )
                    created_products.append(product)
                    logger.info(f"Created product {idx + 1}: {product.item_name} - Rate: {product.rate}, Qty: {product.quantity}, Amount: {product.amount}")
                except Exception as e:
                    logger.error(f"Error creating product {idx + 1}: {str(e)}")
                    # Continue with other products even if one fails

            logger.info(f"Successfully created {len(created_products)} products for bill {zoho_bill.id}")

            # Validate that the sum of product amounts matches the subtotal (if provided)
            if analyzed_data.get('subtotal'):
                try:
                    expected_subtotal = float(analyzed_data['subtotal'])
                    actual_subtotal = sum(float(p.amount) for p in created_products if p.amount)
                    if abs(expected_subtotal - actual_subtotal) > 0.01:  # Allow small rounding differences
                        logger.warning(f"Subtotal mismatch: expected {expected_subtotal}, got {actual_subtotal}")
                except (ValueError, TypeError) as e:
                    logger.warning(f"Could not validate subtotal: {str(e)}")

            return zoho_bill

        except Exception as e:
            logger.error(f"Error creating Zoho objects for bill {bill.id}: {str(e)}")
            raise


class VendorBillSyncView(GenericAPIView):
    """Sync verified vendor bill to Zoho Books. Changes status to 'Synced'."""
    permission_classes = [IsAuthenticated]
    serializer_class = ZohoSyncResponseSerializer

    @extend_schema(
        responses={200: ZohoSyncResponseSerializer},
        tags=["Zoho Vendor Bills"],
    )
    def post(self, request, bill_id):
        organization = get_organization_from_request(request)
        if not organization:
            return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            bill = VendorBill.objects.get(id=bill_id, organization=organization)

            if bill.status != 'Verified':
                return Response(
                    {"detail": "Bill must be in 'Verified' status to sync"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Get Zoho bill data
            try:
                zoho_bill = VendorZohoBill.objects.get(selectBill=bill)
            except VendorZohoBill.DoesNotExist:
                return Response(
                    {"detail": "Zoho bill data not found. Please verify the bill first."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Prepare data for Zoho API
            zoho_data = {
                "vendor_id": zoho_bill.vendor.contactId if zoho_bill.vendor else None,
                "bill_number": zoho_bill.bill_no,
                "date": str(zoho_bill.bill_date) if zoho_bill.bill_date else None,
                "line_items": [],
                "notes": zoho_bill.note
            }

            # Add line items
            for product in zoho_bill.products.all():
                item_data = {
                    "account_id": product.chart_of_accounts.accountId if product.chart_of_accounts else None,
                    "name": product.item_name,
                    "description": product.item_details,
                    "rate": float(product.rate) if product.rate else 0,
                    "quantity": float(product.quantity) if product.quantity else 1,
                    "tax_id": product.taxes.taxId if product.taxes else None
                }
                zoho_data["line_items"].append(item_data)

            # Sync to Zoho Books
            credentials = get_zoho_credentials(organization)
            result = make_zoho_api_request(credentials, "bills", method='POST', data=zoho_data)

            # Update bill status
            bill.status = 'Synced'
            bill.save()

            return Response({
                "detail": "Bill synced to Zoho successfully",
                "zoho_bill_id": result.get('bill', {}).get('bill_id')
            })

        except VendorBill.DoesNotExist:
            return Response({"detail": "Vendor bill not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Sync failed: {str(e)}")
            return Response(
                {"detail": f"Sync failed: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# ============================================================================
# Expense Bill Views
# ============================================================================

class ExpenseBillAnalyzeView(GenericAPIView):
    """Analyze expense bill using OpenAI. Changes status from 'Draft' to 'Analyzed'."""
    permission_classes = [IsAuthenticated]
    serializer_class = AnalysisResponseSerializer

    @extend_schema(
        responses={200: AnalysisResponseSerializer},
        tags=["Zoho Expense Bills"],
    )
    def post(self, request, org_id, bill_id):
        organization = get_organization_from_request(request, org_id=org_id)
        if not organization:
            return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            bill = ExpenseBill.objects.get(id=bill_id, organization=organization)

            if bill.status != 'Draft':
                return Response(
                    {"detail": "Bill must be in 'Draft' status to analyze"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Read file content
            bill.file.seek(0)
            file_content = bill.file.read()
            file_extension = bill.file.name.split('.')[-1].lower()

            # Analyze with OpenAI
            analyzed_data = analyze_bill_with_openai(file_content, file_extension)

            # Update bill with analyzed data
            bill.analysed_data = analyzed_data
            bill.status = 'Analyzed'
            bill.save()

            return Response({
                "detail": "Bill analyzed successfully",
                "analyzed_data": analyzed_data
            })

        except ExpenseBill.DoesNotExist:
            return Response({"detail": "Expense bill not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Analysis failed: {str(e)}")
            return Response(
                {"detail": f"Analysis failed: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ExpenseBillSyncView(GenericAPIView):
    """Sync verified expense bill to Zoho Books. Changes status to 'Synced'."""
    permission_classes = [IsAuthenticated]
    serializer_class = ZohoSyncResponseSerializer

    @extend_schema(
        responses={200: ZohoSyncResponseSerializer},
        tags=["Zoho Expense Bills"],
    )
    def post(self, request, bill_id):
        organization = get_organization_from_request(request)
        if not organization:
            return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            bill = ExpenseBill.objects.get(id=bill_id, organization=organization)

            if bill.status != 'Verified':
                return Response(
                    {"detail": "Bill must be in 'Verified' status to sync"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Get Zoho expense data
            try:
                zoho_bill = ExpenseZohoBill.objects.get(selectBill=bill)
            except ExpenseZohoBill.DoesNotExist:
                return Response(
                    {"detail": "Zoho expense data not found. Please verify the bill first."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Prepare data for Zoho API
            zoho_data = {
                "account_id": zoho_bill.products.first().chart_of_accounts.accountId if zoho_bill.products.exists() else None,
                "date": str(zoho_bill.bill_date) if zoho_bill.bill_date else None,
                "amount": float(zoho_bill.total) if zoho_bill.total else 0,
                "description": zoho_bill.note,
                "vendor_id": zoho_bill.vendor.contactId if zoho_bill.vendor else None
            }

            # Sync to Zoho Books
            credentials = get_zoho_credentials(organization)
            result = make_zoho_api_request(credentials, "expenses", method='POST', data=zoho_data)

            # Update bill status
            bill.status = 'Synced'
            bill.save()

            return Response({
                "detail": "Expense synced to Zoho successfully",
                "zoho_expense_id": result.get('expense', {}).get('expense_id')
            })

        except ExpenseBill.DoesNotExist:
            return Response({"detail": "Expense bill not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Sync failed: {str(e)}")
            return Response(
                {"detail": f"Sync failed: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
