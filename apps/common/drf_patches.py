"""
Monkey patch for Django REST Framework's model_meta utility.

Why this exists
---------------
``model_meta._get_forward_relationships`` walks ``opts.many_to_many``
and reads ``field.remote_field.through._meta.auto_created``. On a few
of this project's models that ``through`` can be ``None`` at the moment
DRF introspects them, which blew up with::

    AttributeError: 'NoneType' object has no attribute '_meta'

The patch below is the ORIGINAL DRF 3.15.2 implementation with a single
guard added around the ``through`` access. Everything else — including
the ``RelationInfo`` namedtuple it returns — is kept byte-for-byte
identical to upstream.

History / do not regress
------------------------
An earlier version of this file returned RAW Django field objects from
``_get_forward_relationships`` instead of ``RelationInfo`` namedtuples.
That silently corrupted ``FieldInfo.relations`` for every model in the
project and produced two downstream crashes that were then papered over
with further patches:

    TypeError: cannot unpack non-iterable ForeignKey object
      (rest_framework/utils/field_mapping.py, get_relation_kwargs)
    AttributeError: 'ForeignKey' object has no attribute 'to_many'
      (ModelSerializer.update)

The compensating ``get_relation_kwargs`` patch never even took effect:
``rest_framework/serializers.py`` binds the name directly at import
time (``from rest_framework.utils.field_mapping import
get_relation_kwargs``), so rebinding the attribute on the
``field_mapping`` module leaves ``serializers.build_relational_field``
calling the original. Hence the 500s on quick-create.

Both compensating patches have been removed — with ``relations``
correct again there is nothing for them to compensate for, and their
silent fallbacks (returning ``read_only=True`` kwargs, or a manual
update path) masked real errors.
"""
import logging

from rest_framework.utils import model_meta
from rest_framework.utils.model_meta import RelationInfo, _get_to_field

logger = logging.getLogger(__name__)

_original_get_forward_relationships = model_meta._get_forward_relationships


def safe_get_forward_relationships(opts):
    """Return a dict of field names to ``RelationInfo``.

    Mirrors DRF 3.15.2's ``_get_forward_relationships`` exactly, except
    that a many-to-many field whose ``through`` model is missing is
    skipped instead of raising ``AttributeError``.
    """
    forward_relations = {}

    # Forward one-to-one / foreign keys — unchanged from upstream.
    for field in [field for field in opts.fields if field.serialize and field.remote_field]:
        forward_relations[field.name] = RelationInfo(
            model_field=field,
            related_model=field.remote_field.model,
            to_many=False,
            to_field=_get_to_field(field),
            has_through_model=False,
            reverse=False,
        )

    # Forward many-to-many — upstream, plus the `through` guard.
    for field in [field for field in opts.many_to_many if field.serialize]:
        through = getattr(getattr(field, 'remote_field', None), 'through', None)
        if through is None or not hasattr(through, '_meta'):
            logger.debug(
                "Skipping ManyToMany field %s.%s — through model is unavailable",
                opts.model_name, field.name,
            )
            continue

        forward_relations[field.name] = RelationInfo(
            model_field=field,
            related_model=field.remote_field.model,
            to_many=True,
            # many-to-many do not have to_fields
            to_field=None,
            has_through_model=(not through._meta.auto_created),
            reverse=False,
        )

    return forward_relations


model_meta._get_forward_relationships = safe_get_forward_relationships
