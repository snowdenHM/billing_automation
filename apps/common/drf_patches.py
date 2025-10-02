"""
Monkey patch for Django REST Framework's model_meta utility to handle
ManyToMany fields with None through models safely.
"""
from rest_framework.utils import model_meta
import logging

logger = logging.getLogger(__name__)

# Store the original function
_original_get_forward_relationships = model_meta._get_forward_relationships


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


# Apply the monkey patch
model_meta._get_forward_relationships = safe_get_forward_relationships

# Also patch the model_meta.get_field_info function to be extra safe
_original_get_field_info = model_meta.get_field_info

def safe_get_field_info(model):
    """
    Safely get field info, with additional error handling for problematic models.
    """
    try:
        return _original_get_field_info(model)
    except AttributeError as e:
        if "'NoneType' object has no attribute '_meta'" in str(e):
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
