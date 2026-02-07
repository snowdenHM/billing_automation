from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from typing import List, Dict, Any
from decimal import Decimal, InvalidOperation
from django.contrib.auth.models import User
from ..models import TallyVendorBill, TallyVendorAnalyzedBill, TallyVendorAnalyzedProduct, Ledger
import logging

logger = logging.getLogger(__name__)


class SafeDecimalField(serializers.DecimalField):
    """Custom decimal field that handles invalid decimal values gracefully"""

    def to_representation(self, value):
        if value is None:
            return None

        try:
            # Convert to Decimal if it's not already
            if not isinstance(value, Decimal):
                value = Decimal(str(value))

            # Check for invalid decimal values
            if value.is_nan() or value.is_infinite():
                return "0.00"

            return super().to_representation(value)
        except (InvalidOperation, ValueError, TypeError):
            # Return 0.00 for any invalid decimal values
            return "0.00"


class UploadedByUserSerializer(serializers.ModelSerializer):
    """Serializer for user information in uploaded_by field"""
    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'email']
        read_only_fields = ['id', 'username', 'first_name', 'last_name', 'email']


class TallyVendorBillSerializer(serializers.ModelSerializer):
    file = serializers.SerializerMethodField()
    uploaded_by = UploadedByUserSerializer(read_only=True)
    uploaded_by_name = serializers.SerializerMethodField()
    bill_belong_your_org = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()

    class Meta:
        model = TallyVendorBill
        fields = [
            'id', 'bill_munshi_name', 'file', 'file_type', 'analysed_data',
            'status', 'process', 'uploaded_by', 'uploaded_by_name', 
            'bill_belong_your_org', 'description',
            'is_duplicate', 'duplicate_description', 'duplicate_score', 'duplicate_matched_bills',
            'is_processing', 'processing_error', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'bill_munshi_name', 'file', 'uploaded_by', 'uploaded_by_name', 'bill_belong_your_org', 'description', 'created_at', 'updated_at']

    def get_file(self, obj):
        """Return complete file URL"""
        if obj.file:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.file.url)
            else:
                # Fallback if no request context
                return obj.file.url
        return None

    def get_uploaded_by_name(self, obj):
        """Return formatted name of the user who uploaded the bill"""
        if obj.uploaded_by:
            if obj.uploaded_by.first_name or obj.uploaded_by.last_name:
                return f"{obj.uploaded_by.first_name} {obj.uploaded_by.last_name}".strip()
            return obj.uploaded_by.username
        return None

    def get_bill_belong_your_org(self, obj):
        """Get bill ownership status - calculate if not set"""
        if obj.bill_belong_your_org is not None:
            return obj.bill_belong_your_org
        
        # Calculate on-the-fly if not set and analysed_data exists
        if obj.analysed_data and obj.organization:
            try:
                validation_result = self._validate_ownership(obj.analysed_data, obj.organization)
                if validation_result and isinstance(validation_result, tuple) and len(validation_result) >= 2:
                    belongs_to_org, _ = validation_result
                    return belongs_to_org
            except Exception as e:
                # Log error but don't break the API
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Error in ownership validation: {str(e)}")
        
        return False

    def get_description(self, obj):
        """Get ownership description - calculate if not set"""
        if obj.description:
            return obj.description
        
        # Calculate on-the-fly if not set and analysed_data exists
        if obj.analysed_data and obj.organization:
            try:
                validation_result = self._validate_ownership(obj.analysed_data, obj.organization)
                if validation_result and isinstance(validation_result, tuple) and len(validation_result) >= 2:
                    _, description = validation_result
                    return description
            except Exception as e:
                # Log error but don't break the API
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Error in ownership validation: {str(e)}")
                return f"Validation error: {str(e)}"
        
        return "Not analyzed yet"

    def _validate_ownership(self, json_data, organization):
        """Private method to validate if organization is the vendor (from field)"""
        try:
            # Null checks for input parameters
            if not json_data or not organization:
                return False, "Missing data or organization"
                
            from_data = json_data.get('from', {})
            if isinstance(from_data, dict):
                vendor_name = from_data.get('name', '').strip()
                vendor_gst = from_data.get('gst_number', '').strip()
                vendor_address = from_data.get('address', '').strip()
            else:
                vendor_name = ''
                vendor_gst = ''
                vendor_address = ''

            # If GST number is missing from extraction, try to find it in the address
            if not vendor_gst and vendor_address:
                import re
                gst_patterns = [
                    r'GST\s*NO\.?\s*:?\s*([A-Z0-9]{15})',
                    r'GSTIN/UIN\s*:?\s*([A-Z0-9]{15})',
                    r'GSTIN\s*:?\s*([A-Z0-9]{15})',
                    r'Tax\s*ID\s*:?\s*([A-Z0-9]{15})',
                    r'UIN\s*:?\s*([A-Z0-9]{15})',
                    r'Registration\s*No\.?\s*:?\s*([A-Z0-9]{15})',
                    r'\b([A-Z0-9]{15})\b'
                ]
                
                for pattern in gst_patterns:
                    match = re.search(pattern, vendor_address.upper(), re.IGNORECASE)
                    if match:
                        vendor_gst = match.group(1).strip()
                        break

            # Check if organization IS the vendor (from field) - bill issued BY organization
            # Priority 1: GST number match
            if vendor_gst and hasattr(organization, 'gst_number') and organization.gst_number:
                org_gst_clean = organization.gst_number.replace(' ', '').replace('-', '').upper()
                vendor_gst_clean = vendor_gst.replace(' ', '').replace('-', '').upper()
                
                if org_gst_clean == vendor_gst_clean:
                    return True, f"✅ Organization GST match: {vendor_gst} - Bill issued BY your organization"
                
                # Partial match for truncated GST numbers
                if len(vendor_gst_clean) >= 10 and len(org_gst_clean) >= 10:
                    if org_gst_clean[:10] == vendor_gst_clean[:10]:
                        return True, f"✅ Partial organization GST match: {vendor_gst} - Bill issued BY your organization"

            # Priority 2: Organization name match with vendor name
            if vendor_name and hasattr(organization, 'name') and organization.name:
                org_name_clean = organization.name.lower().strip()
                vendor_name_clean = vendor_name.lower().strip()
                
                # Exact match
                if org_name_clean == vendor_name_clean:
                    return True, f"✅ Exact organization name match: {vendor_name} - Bill issued BY your organization"
                
                # Partial match
                if org_name_clean in vendor_name_clean or vendor_name_clean in org_name_clean:
                    return True, f"✅ Partial organization name match: {vendor_name} - Bill issued BY your organization"
                
                # Word-based similarity
                org_words = set(org_name_clean.replace(',', '').replace('.', '').split())
                vendor_words = set(vendor_name_clean.replace(',', '').replace('.', '').split())
                
                common_stopwords = {'ltd', 'limited', 'pvt', 'private', 'llp', 'co', 'company', 'inc', 'incorporated'}
                org_words = org_words - common_stopwords
                vendor_words = vendor_words - common_stopwords
                
                if len(org_words) > 0 and len(vendor_words) > 0:
                    overlap = len(org_words.intersection(vendor_words))
                    total_words = len(org_words.union(vendor_words))
                    similarity = overlap / total_words if total_words > 0 else 0
                    
                    if similarity >= 0.6:  # 60% for organization match
                        return True, f"✅ Similar organization name match ({int(similarity*100)}% similarity): {vendor_name} - Bill issued BY your organization"
            
            # Bill NOT issued by organization
            debug_info = f"❌ Bill NOT issued by your organization. Vendor: '{vendor_name}' (GST: '{vendor_gst}') ≠ Your Org: '{getattr(organization, 'name', 'Unknown')}' (GST: '{getattr(organization, 'gst_number', 'None')}')"
            return False, debug_info
            
        except Exception as e:
            return False, f"Validation error: {str(e)}"


class TallyVendorAnalyzedProductSerializer(serializers.ModelSerializer):
    taxes_name = serializers.CharField(source='taxes.name', read_only=True)

    # Use SafeDecimalField for all decimal fields that might have invalid values
    price = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    amount = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    igst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    cgst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    sgst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)

    class Meta:
        model = TallyVendorAnalyzedProduct
        fields = [
            'id', 'item_name', 'item_details', 'taxes', 'taxes_name',
            'price', 'quantity', 'amount', 'product_gst',
            'igst', 'cgst', 'sgst', 'created_at'
        ]
        read_only_fields = ['id', 'created_at', 'taxes_name']


class TallyVendorAnalyzedBillSerializer(serializers.ModelSerializer):
    products = TallyVendorAnalyzedProductSerializer(many=True, read_only=True)
    vendor_name = serializers.CharField(source='vendor.name', read_only=True)
    selected_bill_name = serializers.CharField(source='selected_bill.bill_munshi_name', read_only=True)

    # Use SafeDecimalField for all decimal fields that might have invalid values
    total = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    igst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    cgst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    sgst = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    discount = SafeDecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)

    def get_consolidated_product(self, obj):
        """Get consolidated product data as array for verification flexibility (like Zoho)"""
        try:
            # Ensure obj is a TallyVendorAnalyzedBill instance
            if not hasattr(obj, 'consolidated_products'):
                logger.warning(f"Object {type(obj)} does not have consolidated_products attribute")
                return []
                
            consolidated_products = obj.consolidated_products.all()
            from ..models import TallyVendorConsolidatedProduct

            # Return as array for consistency with Zoho pattern (frontend expects consolidate_prod array)
            return [{
                'id': str(consolidated_product.id),
                'item_name': consolidated_product.item_name,
                'item_details': consolidated_product.item_details,
                'tax_ledger': str(consolidated_product.taxes.name) if consolidated_product.taxes else "No Tax Ledger",
                'price': float(consolidated_product.price or 0),
                'rate': float(consolidated_product.price or 0),  # Alias for price
                'quantity': consolidated_product.quantity or 1,
                'amount': float(consolidated_product.amount or 0),
                'product_gst': consolidated_product.product_gst or "",
                'igst': float(consolidated_product.igst or 0),
                'cgst': float(consolidated_product.cgst or 0),
                'sgst': float(consolidated_product.sgst or 0),
                'original_items_count': consolidated_product.original_items_count or 0,
                'consolidation_notes': consolidated_product.consolidation_notes or "",
                'created_at': consolidated_product.created_at.isoformat() if consolidated_product.created_at else None
            } for consolidated_product in consolidated_products]
        except Exception as e:
            logger.error(f"Error getting consolidated products for {type(obj)}: {str(e)}")
            return []  # Return empty array if no consolidated products exist

    def to_representation(self, instance):
        """Override to include consolidate_prod array like Zoho pattern"""
        try:
            data = super().to_representation(instance)

            # Add consolidate_prod array (matching Zoho pattern for frontend compatibility)
            consolidated_data = self.get_consolidated_product(instance)
            data['consolidate_prod'] = consolidated_data

            return data
        except Exception as e:
            logger.error(f"Error in TallyVendorAnalyzedBillSerializer.to_representation for {type(instance)}: {str(e)}")
            # Return basic data without consolidated_products if there's an error
            data = super().to_representation(instance)
            data['consolidate_prod'] = []
            return data

    class Meta:
        model = TallyVendorAnalyzedBill
        fields = [
            'id', 'selected_bill', 'selected_bill_name', 'vendor', 'vendor_name',
            'bill_no', 'bill_date', 'due_date', 'total', 'igst', 'igst_taxes',
            'cgst', 'cgst_taxes', 'sgst', 'sgst_taxes', 'discount', 'discount_taxes',
            'gst_type', 'note', 'consolidate', 'products', 'created_at'
        ]
        read_only_fields = ['id', 'created_at', 'vendor_name', 'selected_bill_name', 'products']


class VendorBillUploadSerializer(serializers.Serializer):
    """Serializer for single or multiple file upload"""
    files = serializers.ListField(
        child=serializers.FileField(allow_empty_file=False),
        help_text="List of files to upload (PDF, JPG, PNG). Can accept single file or multiple files.",
        allow_empty=False
    )
    file_type = serializers.ChoiceField(
        choices=TallyVendorBill.BillType.choices,
        default=TallyVendorBill.BillType.SINGLE,
        help_text="Type of file upload: SINGLE (each file is a separate bill) or MULTI (PDF pages are split into separate bills)"
    )

    def validate_files(self, value):
        """Validate uploaded files"""
        if not value:
            raise serializers.ValidationError("At least one file is required")

        # Limit total number of files to prevent abuse
        if len(value) > 20:
            raise serializers.ValidationError("Maximum 20 files allowed per upload")

        for file in value:
            # Check file extension
            allowed_extensions = ['.pdf', '.jpg', '.jpeg', '.png']
            file_extension = file.name.lower().split('.')[-1]
            if f'.{file_extension}' not in allowed_extensions:
                raise serializers.ValidationError(
                    f"Unsupported file type: {file.name}. Allowed: {', '.join(allowed_extensions)}"
                )

            # Check file size (10MB per file)
            if file.size > 10 * 1024 * 1024:
                raise serializers.ValidationError(f"File {file.name} exceeds 10MB limit")

        # Additional validation for MULTI type
        file_type = self.initial_data.get('file_type', TallyVendorBill.BillType.SINGLE)
        if file_type == TallyVendorBill.BillType.MULTI:
            # For MULTI type, check if any PDFs are included
            pdf_files = [f for f in value if f.name.lower().endswith('.pdf')]
            if not pdf_files:
                raise serializers.ValidationError("MULTI file type requires at least one PDF file for page splitting")

        return value

    def validate(self, attrs):
        """Cross-field validation"""
        files = attrs.get('files', [])
        file_type = attrs.get('file_type')
        
        # Log the upload attempt
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Vendor bill upload validation - Files: {len(files)}, Type: {file_type}")
        
        return attrs


class BillAnalysisRequestSerializer(serializers.Serializer):
    """Serializer for bill analysis request"""
    bill_id = serializers.UUIDField(help_text="UUID of the bill to analyze")


class BillVerificationSerializer(serializers.Serializer):
    """Serializer for bill verification data"""
    vendor_id = serializers.UUIDField(required=False, allow_null=True)
    bill_no = serializers.CharField(max_length=50, required=False)
    bill_date = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(max_length=500, required=False)
    igst = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    cgst = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    sgst = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    igst_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    cgst_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    sgst_taxes_id = serializers.UUIDField(required=False, allow_null=True)

    products = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        help_text="List of product updates"
    )

    def validate_products(self, value):
        """Validate products data"""
        for product in value:
            if 'id' not in product:
                raise serializers.ValidationError("Product ID is required for each product")
        return value


class BillSyncRequestSerializer(serializers.Serializer):
    """Serializer for bill sync request"""
    bill_id = serializers.UUIDField(help_text="UUID of the bill to sync")


class BillSyncResponseSerializer(serializers.Serializer):
    """Serializer for bill sync response data"""
    id = serializers.UUIDField()
    bill_no = serializers.CharField()
    bill_date = serializers.CharField(allow_null=True)
    total = serializers.FloatField()
    igst = serializers.FloatField()
    cgst = serializers.FloatField()
    sgst = serializers.FloatField()
    vendor = serializers.DictField()
    customer_id = serializers.UUIDField(allow_null=True)
    transactions = serializers.ListField(child=serializers.DictField())


class TallyVendorBillDetailSerializer(serializers.ModelSerializer):
    """Enhanced Tally vendor bill serializer with analyzed data"""

    uploaded_by_username = serializers.CharField(source='uploaded_by.username', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    analyzed_bill = serializers.SerializerMethodField()
    next_bill = serializers.SerializerMethodField()

    def get_analyzed_bill(self, obj):
        """Get analyzed bill data if exists"""
        try:
            from ..models import TallyVendorAnalyzedBill
            analyzed_bill = TallyVendorAnalyzedBill.objects.filter(selected_bill=obj).first()
            if analyzed_bill:
                # Use a try-catch to handle any serialization issues
                try:
                    return TallyVendorAnalyzedBillSerializer(analyzed_bill).data
                except Exception as serialization_error:
                    logger.error(f"Error serializing analyzed bill {analyzed_bill.id}: {str(serialization_error)}")
                    return None
        except Exception as e:
            logger.error(f"Error getting analyzed bill for {obj.id}: {str(e)}")
        return None

    def get_next_bill(self, obj):
        """Get next bill to process"""
        next_bills = TallyVendorBill.objects.filter(
            organization=obj.organization,
            status=TallyVendorBill.BillStatus.ANALYSED
        ).exclude(id=obj.id).values_list('id', flat=True)

        if next_bills:
            import random
            return str(random.choice(list(next_bills)))
        return None

    class Meta:
        model = TallyVendorBill
        fields = [
            'id', 'bill_munshi_name', 'file', 'file_type', 'analysed_data',
            'status', 'process', 'uploaded_by', 'uploaded_by_username',
            'organization_name', 'created_at', 'updated_at', 'analyzed_bill', 'next_bill'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'uploaded_by_username', 'organization_name', 'analyzed_bill', 'next_bill']
