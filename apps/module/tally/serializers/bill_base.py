# apps/module/tally/serializers/bill_base.py
"""
Shared base serializers for Tally vendor and expense bills.
Eliminates duplication by providing common functionality.
"""
import logging
from decimal import Decimal, InvalidOperation

from rest_framework import serializers

from apps.common.serializers import UploadedByUserSerializer

logger = logging.getLogger(__name__)


# ============================================================================
# Safe Decimal Field
# ============================================================================

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


# ============================================================================
# Base Bill Serializer (List View)
# ============================================================================

class BaseTallyBillSerializer(serializers.ModelSerializer):
    """
    Base serializer for Tally bill list views.
    
    Subclasses must define:
        - Meta.model
        - Meta.fields (can extend base_fields)
    """
    
    file = serializers.SerializerMethodField()
    uploaded_by = UploadedByUserSerializer(read_only=True)
    uploaded_by_name = serializers.SerializerMethodField()
    bill_belong_your_org = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()

    # Common fields that all bill serializers share
    base_fields = [
        'id', 'bill_munshi_name', 'file', 'file_type', 'analysed_data',
        'status', 'process', 'uploaded_by', 'uploaded_by_name',
        'bill_belong_your_org', 'description',
        'is_duplicate', 'duplicate_description', 'duplicate_score', 'duplicate_matched_bills',
        'is_processing', 'processing_error',
        'tally_synced', 'tally_sync_message',
        'created_at', 'updated_at'
    ]

    base_read_only_fields = [
        'id', 'bill_munshi_name', 'file', 'uploaded_by', 'uploaded_by_name', 
        'bill_belong_your_org', 'description', 'created_at', 'updated_at'
    ]

    def get_file(self, obj):
        """Return complete file URL"""
        if obj.file:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.file.url)
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
                logger.error(f"Error in ownership validation: {str(e)}")
                return f"Validation error: {str(e)}"
        
        return "Not analyzed yet"

    @staticmethod
    def _validate_ownership(json_data, organization):
        """Delegate to shared ownership validation service."""
        from apps.common.services.ownership import validate_bill_ownership_simple
        return validate_bill_ownership_simple(json_data, organization, check_field='from')


# ============================================================================
# Base Bill Upload Serializer
# ============================================================================

class BaseBillUploadSerializer(serializers.Serializer):
    """
    Base serializer for file upload validation.
    
    Subclasses must define:
        - bill_model: The bill model class (for BillType choices)
    """
    
    bill_model = None  # Override in subclass
    
    files = serializers.ListField(
        child=serializers.FileField(allow_empty_file=False),
        help_text="List of files to upload (PDF, JPG, PNG). Can accept single file or multiple files.",
        allow_empty=False
    )
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Dynamically set file_type choices based on bill_model
        if self.bill_model:
            self.fields['file_type'] = serializers.ChoiceField(
                choices=self.bill_model.BillType.choices,
                default=self.bill_model.BillType.SINGLE,
                help_text="Type of file upload: SINGLE (each file is separate bill) or MULTI (PDF pages split)"
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
        if self.bill_model:
            file_type = self.initial_data.get('file_type', self.bill_model.BillType.SINGLE)
            if file_type == self.bill_model.BillType.MULTI:
                pdf_files = [f for f in value if f.name.lower().endswith('.pdf')]
                if not pdf_files:
                    raise serializers.ValidationError(
                        "MULTI file type requires at least one PDF file for page splitting"
                    )

        return value

    def validate(self, attrs):
        """Cross-field validation"""
        files = attrs.get('files', [])
        file_type = attrs.get('file_type')
        logger.info(f"Bill upload validation - Files: {len(files)}, Type: {file_type}")
        return attrs


# ============================================================================
# Simple Request/Response Serializers (Shared)
# ============================================================================

class BillAnalysisRequestSerializer(serializers.Serializer):
    """Serializer for bill analysis request"""
    bill_id = serializers.UUIDField(help_text="UUID of the bill to analyze")


class BillSyncRequestSerializer(serializers.Serializer):
    """Serializer for bill sync request"""
    bill_id = serializers.UUIDField(help_text="UUID of the bill to sync")


# ============================================================================
# Base Bill Detail Serializer
# ============================================================================

class BaseBillDetailSerializer(serializers.ModelSerializer):
    """
    Base serializer for bill detail views with analyzed data.
    
    Subclasses must define:
        - Meta.model
        - Meta.fields
        - analyzed_bill_model (class attribute)
        - analyzed_bill_serializer (class attribute)
    """
    
    analyzed_bill_model = None  # Override in subclass
    analyzed_bill_serializer = None  # Override in subclass
    
    uploaded_by_username = serializers.CharField(source='uploaded_by.username', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    analyzed_bill = serializers.SerializerMethodField()
    next_bill = serializers.SerializerMethodField()

    # Common fields for all detail serializers
    base_fields = [
        'id', 'bill_munshi_name', 'file', 'file_type', 'analysed_data',
        'status', 'process', 'uploaded_by', 'uploaded_by_username',
        'organization_name', 'created_at', 'updated_at', 'analyzed_bill', 'next_bill'
    ]

    base_read_only_fields = [
        'id', 'created_at', 'updated_at', 'uploaded_by_username', 
        'organization_name', 'analyzed_bill', 'next_bill'
    ]

    def get_analyzed_bill(self, obj):
        """Get analyzed bill data if exists"""
        if not self.analyzed_bill_model or not self.analyzed_bill_serializer:
            return None
            
        try:
            analyzed_bill = self.analyzed_bill_model.objects.filter(selected_bill=obj).first()
            if analyzed_bill:
                try:
                    return self.analyzed_bill_serializer(analyzed_bill).data
                except Exception as e:
                    logger.error(f"Error serializing analyzed bill {analyzed_bill.id}: {str(e)}")
                    return None
        except Exception as e:
            logger.error(f"Error getting analyzed bill for {obj.id}: {str(e)}")
        return None

    def get_next_bill(self, obj):
        """Get next bill to process (oldest analysed bill)."""
        bill_model = obj.__class__
        next_bill = (
            bill_model.objects.filter(
                organization=obj.organization,
                status=bill_model.BillStatus.ANALYSED,
            )
            .exclude(id=obj.id)
            .order_by('created_at')
            .values_list('id', flat=True)
            .first()
        )
        return str(next_bill) if next_bill else None
