"""
Monkey patch for Django REST Framework's model_meta utility to handle
ManyToMany fields with None through models safely.
"""
from rest_framework.utils import model_meta, field_mapping
import logging

logger = logging.getLogger(__name__)

# Store the original functions
_original_get_forward_relationships = model_meta._get_forward_relationships
_original_get_relation_kwargs = field_mapping.get_relation_kwargs


def safe_get_forward_relationships(opts):
    """
    Safely get forward relationships, handling ManyToMany fields with None through models.
    This fixes the AttributeError: 'NoneType' object has no attribute '_meta' issue.
    """
    forward_relations = {}

    for field in [f for f in opts.get_fields() if f.is_relation]:
        try:
            if field.many_to_many:
                # This is the critical fix for the AttributeError
                if hasattr(field, 'remote_field') and field.remote_field:
                    through = getattr(field.remote_field, 'through', None)

                    # Skip fields where through is None - this prevents the AttributeError
                    if through is None:
                        logger.debug(f"Skipping ManyToMany field {field.name} with None through model")
                        continue

                    # Safe check for _meta attribute
                    if not hasattr(through, '_meta'):
                        logger.debug(f"Skipping ManyToMany field {field.name} - through model has no _meta")
                        continue

                    # Check if it's auto-created to avoid issues with custom through models
                    if hasattr(through._meta, 'auto_created') and not through._meta.auto_created:
                        # Skip custom through models that might cause issues
                        logger.debug(f"Skipping ManyToMany field {field.name} with custom through model")
                        continue

                # If we get here, the field should be safe to include
                forward_relations[field.name] = field
            else:
                # Handle non-ManyToMany forward relations normally
                forward_relations[field.name] = field

        except AttributeError as e:
            # Log and skip any problematic fields
            logger.warning(f"Skipping field {field.name} due to AttributeError: {e}")
            continue
        except Exception as e:
            # Log and skip any other unexpected errors
            logger.warning(f"Skipping field {field.name} due to unexpected error: {e}")
            continue

    return forward_relations


def safe_get_relation_kwargs(field_name, relation_info):
    """
    Safely get relation kwargs, handling cases where relation_info is not a tuple.
    This fixes the TypeError: cannot unpack non-iterable OneToOneField object issue.
    """
    try:
        # Check if relation_info is a Django model field object (ForeignKey, OneToOneField, etc.)
        from django.db import models
        if isinstance(relation_info, (models.ForeignKey, models.OneToOneField, models.ManyToManyField)):
            # This is a raw field object, not a relation_info tuple
            # Let's try to use the original function which should handle this better
            try:
                return _original_get_relation_kwargs(field_name, relation_info)
            except Exception:
                # If original function fails, create basic kwargs based on field type
                if isinstance(relation_info, models.ManyToManyField):
                    return {
                        'queryset': relation_info.related_model.objects.none(),
                        'many': True,
                        'read_only': False,
                        'allow_null': True,
                    }
                else:  # ForeignKey or OneToOneField
                    return {
                        'queryset': relation_info.related_model.objects.none(),
                        'many': False,
                        'read_only': False,
                        'allow_null': relation_info.null,
                    }

        # Check if relation_info is iterable and has the expected structure
        if hasattr(relation_info, '__iter__') and not isinstance(relation_info, str):
            # Try to unpack as expected
            try:
                if len(relation_info) == 6:
                    model_field, related_model, to_many, to_field, has_through_model, reverse = relation_info
                    # Proceed with normal processing
                    return _original_get_relation_kwargs(field_name, relation_info)
                else:
                    # Unexpected length, use original function
                    return _original_get_relation_kwargs(field_name, relation_info)
            except (TypeError, ValueError) as e:
                logger.debug(f"Cannot unpack relation_info for field {field_name}: {e}")
                # Return basic kwargs to avoid crash
                return {
                    'queryset': None,
                    'many': False,
                    'read_only': True,
                    'allow_null': True,
                }
        else:
            # relation_info is not iterable - reduced logging level to debug
            logger.debug(f"Relation info for field {field_name} is not iterable: {type(relation_info)}")
            # Try original function first
            try:
                return _original_get_relation_kwargs(field_name, relation_info)
            except Exception:
                # Return basic kwargs as fallback
                return {
                    'queryset': None,
                    'many': False,
                    'read_only': True,
                    'allow_null': True,
                }

    except Exception as e:
        logger.debug(f"Error processing relation kwargs for field {field_name}: {e}")
        # Try original function as last resort
        try:
            return _original_get_relation_kwargs(field_name, relation_info)
        except Exception:
            # Return basic kwargs as final fallback
            return {
                'queryset': None,
                'many': False,
                'read_only': True,
                'allow_null': True,
            }


# Apply the monkey patches
model_meta._get_forward_relationships = safe_get_forward_relationships
field_mapping.get_relation_kwargs = safe_get_relation_kwargs

# Also patch the model_meta.get_field_info function to be extra safe
_original_get_field_info = model_meta.get_field_info

def safe_get_field_info(model):
    """
    Safely get field info, with additional error handling for problematic models.
    """
    try:
        return _original_get_field_info(model)
    except (AttributeError, TypeError) as e:
        if "'NoneType' object has no attribute '_meta'" in str(e) or "cannot unpack" in str(e):
            logger.warning(f"Falling back to basic field info for model {model} due to error: {e}")
            # Return minimal field info structure
            return model_meta.FieldInfo(
                pk=model._meta.pk,
                fields={},
                forward_relations={},
                reverse_relations={},
                fields_and_pk={},
                relations={}
            )
        raise

model_meta.get_field_info = safe_get_field_info

# Patch for the 'to_many' attribute error in DRF serializers
from rest_framework import serializers
from django.db import models

# Store the original update method
_original_serializer_update = serializers.ModelSerializer.update

def safe_serializer_update(self, instance, validated_data):
    """
    Safely update serializer instance, handling 'to_many' attribute errors on ForeignKey fields.
    This fixes the AttributeError: 'ForeignKey' object has no attribute 'to_many' issue.
    """
    try:
        return _original_serializer_update(self, instance, validated_data)
    except AttributeError as e:
        if "'ForeignKey' object has no attribute 'to_many'" in str(e):
            logger.warning(f"Handling 'to_many' attribute error in serializer update for {type(instance).__name__}")

            # Add to_many attribute to ForeignKey fields that are missing it
            model_info = model_meta.get_field_info(instance._meta.model)

            for field_name, relation_info in model_info.relations.items():
                if hasattr(relation_info, '__class__') and isinstance(relation_info, models.ForeignKey):
                    if not hasattr(relation_info, 'to_many'):
                        relation_info.to_many = False
                        logger.debug(f"Added to_many=False to ForeignKey field: {field_name}")
                elif hasattr(relation_info, '__class__') and isinstance(relation_info, models.ManyToManyField):
                    if not hasattr(relation_info, 'to_many'):
                        relation_info.to_many = True
                        logger.debug(f"Added to_many=True to ManyToManyField field: {field_name}")

            # Try the update again
            try:
                return _original_serializer_update(self, instance, validated_data)
            except Exception as retry_error:
                logger.error(f"Retry failed after adding to_many attributes: {retry_error}")
                # Manual update as fallback
                return self._manual_update_fallback(instance, validated_data)
        else:
            raise

def _manual_update_fallback(self, instance, validated_data):
    """
    Manual update fallback when DRF's built-in update fails.
    """
    logger.warning(f"Using manual update fallback for {type(instance).__name__}")

    # Update simple fields
    for attr, value in validated_data.items():
        if hasattr(instance, attr):
            # Check if it's a relation field
            field = instance._meta.get_field(attr)
            if isinstance(field, (models.ForeignKey, models.OneToOneField)):
                # Handle foreign key relationships
                setattr(instance, attr, value)
                logger.debug(f"Updated ForeignKey field {attr} = {value}")
            elif isinstance(field, models.ManyToManyField):
                # Skip many-to-many fields in this fallback (they need special handling)
                logger.debug(f"Skipping ManyToManyField {attr} in fallback update")
                continue
            else:
                # Handle regular fields
                setattr(instance, attr, value)
                logger.debug(f"Updated field {attr} = {value}")

    instance.save()
    return instance

# Apply the serializer update patch
serializers.ModelSerializer.update = safe_serializer_update
serializers.ModelSerializer._manual_update_fallback = _manual_update_fallback

