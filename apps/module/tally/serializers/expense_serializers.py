from rest_framework import serializers
from django.contrib.auth.models import User
from drf_spectacular.utils import extend_schema_field
from ..models import TallyExpenseBill, TallyExpenseAnalyzedBill, TallyExpenseAnalyzedProduct
import logging

logger = logging.getLogger(__name__)


class UploadedByUserSerializer(serializers.ModelSerializer):
    """Serializer for user information in uploaded_by field"""
    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'email']
        read_only_fields = ['id', 'username', 'first_name', 'last_name', 'email']


class TallyExpenseBillSerializer(serializers.ModelSerializer):
    file = serializers.SerializerMethodField()
    uploaded_by = UploadedByUserSerializer(read_only=True)
    uploaded_by_name = serializers.SerializerMethodField()
    bill_belong_your_org = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()

    class Meta:
        model = TallyExpenseBill
        fields = [
            'id', 'bill_munshi_name', 'file', 'file_type', 'analysed_data',
            'status', 'process', 'uploaded_by', 'uploaded_by_name', 
            'bill_belong_your_org', 'description',
            'is_duplicate', 'duplicate_description', 'duplicate_score', 'duplicate_matched_bills',
            'is_processing', 'processing_error', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'bill_munshi_name', 'file', 'uploaded_by', 'uploaded_by_name', 'bill_belong_your_org', 'description', 'created_at', 'updated_at']

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_file(self, obj) -> str | None:
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

    def validate_file(self, value):
        """Validate file extension and size"""
        if value:
            # Check file extension
            allowed_extensions = ['.pdf', '.jpg', '.jpeg', '.png']
            file_extension = value.name.lower().split('.')[-1]
            if f'.{file_extension}' not in allowed_extensions:
                raise serializers.ValidationError(
                    f"Unsupported file type. Allowed types: {', '.join(allowed_extensions)}"
                )

            # Check file size (10MB limit)
            if value.size > 10 * 1024 * 1024:
                raise serializers.ValidationError("File size cannot exceed 10MB")

        return value


class TallyExpenseAnalyzedProductSerializer(serializers.ModelSerializer):
    chart_of_accounts_name = serializers.CharField(source='chart_of_accounts.name', read_only=True)

    class Meta:
        model = TallyExpenseAnalyzedProduct
        fields = [
            'id', 'item_details', 'chart_of_accounts', 'chart_of_accounts_name',
            'amount', 'debit_or_credit', 'created_at'
        ]
        read_only_fields = ['id', 'created_at', 'chart_of_accounts_name']


class TallyExpenseAnalyzedBillSerializer(serializers.ModelSerializer):
    products = TallyExpenseAnalyzedProductSerializer(many=True, read_only=True)
    vendor_name = serializers.CharField(source='vendor.name', read_only=True)
    selected_bill_name = serializers.CharField(source='selected_bill.bill_munshi_name', read_only=True)

    def get_consolidate_prod_data(self, obj):
        """Get consolidated product data if exists"""
        try:
            # Ensure obj is a TallyExpenseAnalyzedBill instance
            if not hasattr(obj, 'consolidated_products'):
                logger.warning(f"Object {type(obj)} does not have consolidated_products attribute")
                return []
                
            consolidated_products = obj.consolidated_products.all()
            from ..models import TallyExpenseConsolidatedProduct

            return [{
                'id': str(consolidated_product.id),
                'item_details': consolidated_product.item_details,
                'chart_of_accounts': str(consolidated_product.chart_of_accounts.name) if consolidated_product.chart_of_accounts else "No COA Ledger",
                'amount': float(consolidated_product.amount or 0),
                'debit_or_credit': consolidated_product.debit_or_credit or "debit",
                'original_entries_count': consolidated_product.original_entries_count or 0,
                'consolidation_notes': consolidated_product.consolidation_notes or "",
                'created_at': consolidated_product.created_at.isoformat() if consolidated_product.created_at else None
            } for consolidated_product in consolidated_products]
        except Exception as e:
            logger.error(f"Error getting consolidated products for {type(obj)}: {str(e)}")
            return []

    def to_representation(self, instance):
        """Override to include consolidate_prod array like Zoho pattern"""
        try:
            data = super().to_representation(instance)

            # Add consolidate_prod array (matching Zoho pattern for frontend compatibility)
            consolidated_data = self.get_consolidate_prod_data(instance)
            data['consolidate_prod'] = consolidated_data

            return data
        except Exception as e:
            logger.error(f"Error in TallyExpenseAnalyzedBillSerializer.to_representation for {type(instance)}: {str(e)}")
            # Return basic data without consolidated_products if there's an error
            data = super().to_representation(instance)
            data['consolidate_prod'] = []
            return data

    class Meta:
        model = TallyExpenseAnalyzedBill
        fields = [
            'id', 'selected_bill', 'selected_bill_name', 'vendor', 'vendor_name',
            'voucher', 'bill_no', 'bill_date', 'due_date', 'total', 'vendor_amount', 'vendor_debit_or_credit',
            'igst', 'igst_taxes', 'igst_debit_or_credit', 'cgst', 'cgst_taxes', 'cgst_debit_or_credit',
            'sgst', 'sgst_taxes', 'sgst_debit_or_credit', 'tds', 'tds_taxes', 'tds_debit_or_credit',
            'other_adjustment', 'other_adjustment_taxes', 'other_adjustment_debit_or_credit',
            'note', 'consolidate', 'products', 'created_at'
        ]
        read_only_fields = ['id', 'created_at', 'vendor_name', 'selected_bill_name', 'products']


class ExpenseBillUploadSerializer(serializers.Serializer):
    """Serializer for single or multiple file upload"""
    files = serializers.ListField(
        child=serializers.FileField(allow_empty_file=False),
        help_text="List of files to upload (PDF, JPG, PNG). Can accept single file or multiple files.",
        allow_empty=False
    )
    file_type = serializers.ChoiceField(
        choices=TallyExpenseBill.BillType.choices,
        default=TallyExpenseBill.BillType.SINGLE,
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
        file_type = self.initial_data.get('file_type', TallyExpenseBill.BillType.SINGLE)
        if file_type == TallyExpenseBill.BillType.MULTI:
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
        logger.info(f"Expense bill upload validation - Files: {len(files)}, Type: {file_type}")
        
        return attrs


class ExpenseBillAnalysisRequestSerializer(serializers.Serializer):
    """Serializer for expense bill analysis request"""
    bill_id = serializers.UUIDField(help_text="UUID of the expense bill to analyze")


class ExpenseBillVerificationSerializer(serializers.Serializer):
    """Serializer for expense bill verification data"""
    vendor_id = serializers.UUIDField(required=False, allow_null=True)
    voucher = serializers.CharField(max_length=255, required=False)
    bill_no = serializers.CharField(max_length=50, required=False)
    bill_date = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(max_length=500, required=False)
    igst = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    cgst = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    sgst = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    tds = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    other_adjustment = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    igst_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    cgst_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    sgst_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    tds_taxes_id = serializers.UUIDField(required=False, allow_null=True)
    other_adjustment_taxes_id = serializers.UUIDField(required=False, allow_null=True)

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


class ExpenseBillSyncRequestSerializer(serializers.Serializer):
    """Serializer for expense bill sync request"""
    bill_id = serializers.UUIDField(help_text="UUID of the expense bill to sync")


class ExpenseBillSyncResponseSerializer(serializers.Serializer):
    """Serializer for expense bill sync response data"""
    id = serializers.CharField()
    voucher = serializers.CharField()
    bill_no = serializers.CharField()
    bill_date = serializers.CharField(allow_null=True)
    total = serializers.FloatField()
    name = serializers.CharField()
    company = serializers.CharField()
    gst_in = serializers.CharField()
    DR_LEDGER = serializers.ListField(child=serializers.DictField())
    CR_LEDGER = serializers.ListField(child=serializers.DictField())
    note = serializers.CharField()


class TallyExpenseBillDetailSerializer(serializers.ModelSerializer):
    """Enhanced Tally expense bill serializer with analyzed data"""

    uploaded_by_username = serializers.CharField(source='uploaded_by.username', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    analyzed_bill = serializers.SerializerMethodField()
    next_bill = serializers.SerializerMethodField()

    def get_analyzed_bill(self, obj):
        """Get analyzed bill data if exists"""
        try:
            from ..models import TallyExpenseAnalyzedBill
            analyzed_bill = TallyExpenseAnalyzedBill.objects.filter(selected_bill=obj).first()
            if analyzed_bill:
                return TallyExpenseAnalyzedBillSerializer(analyzed_bill).data
        except:
            pass
        return None

    def get_next_bill(self, obj):
        """Get next bill to process"""
        next_bills = TallyExpenseBill.objects.filter(
            organization=obj.organization,
            status=TallyExpenseBill.BillStatus.ANALYSED
        ).exclude(id=obj.id).values_list('id', flat=True)

        if next_bills:
            import random
            return str(random.choice(list(next_bills)))
        return None

    class Meta:
        model = TallyExpenseBill
        fields = [
            'id', 'bill_munshi_name', 'file', 'file_type', 'analysed_data',
            'status', 'process', 'uploaded_by', 'uploaded_by_username',
            'organization_name', 'created_at', 'updated_at', 'analyzed_bill', 'next_bill'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'uploaded_by_username', 'organization_name', 'analyzed_bill', 'next_bill']
