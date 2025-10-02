"""
Custom DRF Spectacular preprocessors to handle problematic model relationships
and fix schema generation issues.
"""
from drf_spectacular.extensions import OpenApiSerializerExtension
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers
import logging

logger = logging.getLogger(__name__)


class SafeManyToManyFieldExtension(OpenApiSerializerExtension):
    """
    Extension to safely handle ManyToMany fields that might have None through models.
    """
    target_class = 'rest_framework.relations.ManyRelatedField'

    def map_serializer_field(self, auto_schema, direction):
        """
        Custom mapping for ManyToMany fields to prevent None through model errors.
        """
        try:
            return super().map_serializer_field(auto_schema, direction)
        except AttributeError as e:
            if "'NoneType' object has no attribute '_meta'" in str(e):
                logger.warning(f"Skipping problematic ManyToMany field due to None through model: {e}")
                # Return a basic array schema as fallback
                return {
                    'type': 'array',
                    'items': {'type': 'string'}
                }
            raise


class SafeAutoSchema(AutoSchema):
    """
    Custom AutoSchema that safely handles model introspection errors.
    """

    def _get_serializer_field_meta(self, field, direction):
        """
        Override to safely handle field metadata extraction.
        """
        try:
            return super()._get_serializer_field_meta(field, direction)
        except AttributeError as e:
            if "'NoneType' object has no attribute '_meta'" in str(e):
                logger.warning(f"Skipping field metadata extraction due to error: {e}")
                return {}
            raise

    def _map_serializer_field(self, field, direction, bypass_extensions=False):
        """
        Override to safely handle field mapping.
        """
        try:
            return super()._map_serializer_field(field, direction, bypass_extensions)
        except AttributeError as e:
            if "'NoneType' object has no attribute '_meta'" in str(e):
                logger.warning(f"Skipping field mapping due to error: {e}")
                # Return a basic string schema as fallback
                return {
                    'type': 'string',
                    'description': 'Field mapping skipped due to model introspection error'
                }
            raise


def preprocess_exclude_problematic_models(endpoints):
    """
    Preprocessor to exclude or modify endpoints that cause schema generation issues.
    """
    filtered_endpoints = []

    for path, path_regex, method, callback in endpoints:
        try:
            # Skip problematic endpoints that we know cause issues
            if hasattr(callback, 'cls'):
                view_class = callback.cls
                # You can add specific view classes to skip here if needed
                if hasattr(view_class, '__name__') and view_class.__name__ in [
                    # Add problematic view names here if needed
                ]:
                    logger.warning(f"Skipping problematic endpoint: {path} {method}")
                    continue

            filtered_endpoints.append((path, path_regex, method, callback))
        except Exception as e:
            logger.warning(f"Error processing endpoint {path} {method}: {e}")
            # Skip problematic endpoints
            continue

    return filtered_endpoints


# Safe fallback serializer for views that can't be introspected
class SafeFallbackSerializer(serializers.Serializer):
    """
    Fallback serializer for views that cause introspection issues.
    """
    message = serializers.CharField(default="Response data unavailable due to introspection issues")
    data = serializers.DictField(required=False)
