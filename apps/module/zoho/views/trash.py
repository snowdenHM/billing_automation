"""Zoho Trash API — list, restore, permanently delete and empty.

The Zoho mirror of ``apps/module/tally/views/trash.py``. Deleting from a
Zoho bill list moves the bill here (see ``zoho_bill_delete_base``); this
module is what the Trash page talks to for the Zoho half.

Permissions follow the same "safety net stays useful" rule as Tally:
moving to trash and restoring sit on ``TRASH_BILLS`` so an Accountant can
undo their own mistake, while destroying a bill for good is
``DELETE_BILLS`` and additionally admin-only.
"""

import logging

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common import roles
from apps.common.models import BillActivity
from apps.common.pagination import DefaultPagination
from apps.common.permissions import HasOrgPermission, requires
from apps.common.services.activity import record_activity
from apps.common.utils import get_organization_from_request
from apps.common.views import generate_signed_bill_file_url
from apps.module.zoho.trash import (
    MODULE,
    MODULE_LABEL,
    TRASH_KINDS,
    get_kind,
    purge_bill,
    retention_summary,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Helpers
# ============================================================================

def _org_not_found(org_id):
    return Response(
        {
            'error': 'Organization Access Denied',
            'message': f'Organization with ID {org_id} not found or you do not have access to it.',
            'error_code': 'ORG_NOT_FOUND',
        },
        status=status.HTTP_404_NOT_FOUND,
    )


def _is_org_admin(user, organization):
    """Whether *user* may permanently destroy documents in *organization*."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    return organization.memberships.filter(
        user=user, role="ADMIN", is_active=True
    ).exists()


def _forbidden_response():
    return Response(
        {
            'error': 'Permission denied',
            'message': 'Only an organization admin can permanently delete documents.',
            'error_code': 'ADMIN_REQUIRED',
        },
        status=status.HTTP_403_FORBIDDEN,
    )


def _person(user):
    """Compact {id, name} for the uploader / deleter columns."""
    if not user:
        return None
    name = f"{user.first_name} {user.last_name}".strip() or user.username or user.email
    return {'id': str(user.id), 'name': name, 'email': user.email}


def _serialize(bill, kind, request):
    """Flatten one trashed bill into the shape the Trash page renders.

    ``module`` / ``module_label`` are what keep a Zoho purchase voucher
    distinguishable from a Tally one: both modules use the slug
    ``purchase-voucher``, so the slug alone is ambiguous once the screen
    merges the two feeds.
    """
    return {
        'id': str(bill.id),
        'module': MODULE,
        'module_label': MODULE_LABEL,
        'type': kind.slug,
        'type_label': kind.label,
        'list_path': kind.list_path,
        # Zoho names the document field in camelCase; Tally uses
        # bill_munshi_name. Both are flattened to `name` here so the
        # Trash table renders one column for either module.
        'name': bill.billmunshiName,
        'status': bill.status,
        'file': generate_signed_bill_file_url(bill.file, request=request) if bill.file else None,
        'uploaded_by': _person(bill.uploaded_by),
        'created_at': bill.created_at,
        'deleted_at': bill.deleted_at,
        'deleted_by': _person(bill.deleted_by),
        'purge_at': bill.purge_at,
        'days_until_purge': bill.days_until_purge,
    }


def _kinds_for(request):
    """Resolve the ``?type=`` filter to the kinds a request should cover.

    Returns ``(kinds, error_response)``.
    """
    requested = (request.query_params.get('type') or '').strip().lower()
    if not requested or requested == 'all':
        return list(TRASH_KINDS.values()), None

    kind = get_kind(requested)
    if not kind:
        return None, Response(
            {
                'error': 'Unknown document type',
                'message': f"'{requested}' is not a trashable Zoho document type.",
                'valid_types': sorted(TRASH_KINDS),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    return [kind], None


# ============================================================================
# List
# ============================================================================

@extend_schema(
    summary="List trashed Zoho bills",
    description=(
        "Zoho bills moved to trash and still inside the retention window, "
        "newest deletion first. Expired bills are excluded even if the purge "
        "job has not run yet."
    ),
    parameters=[
        OpenApiParameter(
            'type', str,
            description=(
                'purchase-voucher | journal-entry | expense-bill | '
                'sales-voucher | receipt-voucher | payment-voucher | all'
            ),
        ),
        OpenApiParameter('search', str, description='Match against the document name'),
    ],
    tags=['Zoho Trash'],
)
@requires(roles.VIEW_BILLS)
@api_view(['GET'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def trash_list(request, org_id):
    """Unified trash listing across every trashable Zoho bill type."""
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _org_not_found(org_id)

    kinds, error = _kinds_for(request)
    if error:
        return error

    search = (request.query_params.get('search') or '').strip()

    # The six bill types live in separate tables, so the merge happens in
    # Python. Bounded by the retention window, a single org's trash stays
    # small enough for this to be cheaper than a UNION across models whose
    # columns don't line up.
    rows = []
    counts = {}
    for kind in kinds:
        qs = (
            kind.model.objects.recoverable()
            .filter(organization=organization)
            .select_related('uploaded_by', 'deleted_by')
        )
        if search:
            qs = qs.filter(billmunshiName__icontains=search)

        bills = list(qs)
        counts[kind.slug] = len(bills)
        rows.extend((bill, kind) for bill in bills)

    # Newest deletion first. deleted_at is always set by move_to_trash, but
    # a row trashed by an older path could be null — sort those last rather
    # than blowing up on a None comparison.
    rows.sort(key=lambda pair: (pair[0].deleted_at is not None, pair[0].deleted_at), reverse=True)

    paginator = DefaultPagination()
    page = paginator.paginate_queryset(rows, request)
    payload = [_serialize(bill, kind, request) for bill, kind in (page if page is not None else rows)]

    summary = retention_summary()
    extra = {
        'module': MODULE,
        'retention_days': summary['retention_days'],
        'counts_by_type': counts,
        'types': [
            {
                'slug': k.slug,
                'label': k.label,
                'list_path': k.list_path,
                'module': MODULE,
                'module_label': MODULE_LABEL,
            }
            for k in TRASH_KINDS.values()
        ],
    }

    if page is not None:
        response = paginator.get_paginated_response(payload)
        response.data.update(extra)
        return response

    return Response({'count': len(payload), 'results': payload, **extra})


# ============================================================================
# Restore
# ============================================================================

@extend_schema(
    summary="Restore a trashed Zoho bill",
    description="Bring a bill back out of the trash. Anyone who may trash may restore.",
    tags=['Zoho Trash'],
)
@requires(roles.TRASH_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def trash_restore(request, org_id, kind_slug, bill_id):
    """Restore one bill from the trash back onto its normal list."""
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _org_not_found(org_id)

    kind = get_kind(kind_slug)
    if not kind:
        return Response(
            {'error': 'Unknown document type', 'valid_types': sorted(TRASH_KINDS)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # `recoverable()` not `trashed()`: an expired bill is on its way out and
    # must not be resurrected just because the purge job hasn't reached it.
    try:
        bill = kind.model.objects.recoverable().get(id=bill_id, organization=organization)
    except kind.model.DoesNotExist:
        return Response(
            {'error': 'Bill not found in trash',
             'message': 'It may already have been restored or purged.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    bill.restore()
    record_activity(
        bill, BillActivity.Action.RESTORED,
        actor=request.user, organization=organization,
        summary="Restored from Trash",
    )
    logger.info("Zoho trash: restored %s %s in org %s", kind.slug, bill.pk, organization.id)

    return Response({
        'message': f'{kind.label} restored',
        'id': str(bill.id),
        'module': MODULE,
        'type': kind.slug,
        'list_path': kind.list_path,
    })


# ============================================================================
# Permanent delete (admin only)
# ============================================================================

@extend_schema(
    summary="Permanently delete a trashed Zoho bill",
    description="Destroys the bill row and its uploaded file. Admin only. Cannot be undone.",
    tags=['Zoho Trash'],
)
@requires(roles.DELETE_BILLS)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def trash_delete_forever(request, org_id, kind_slug, bill_id):
    """Destroy one trashed bill for good."""
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _org_not_found(org_id)

    if not _is_org_admin(request.user, organization):
        return _forbidden_response()

    kind = get_kind(kind_slug)
    if not kind:
        return Response(
            {'error': 'Unknown document type', 'valid_types': sorted(TRASH_KINDS)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # `trashed()` here, unlike restore — an expired bill should still be
    # destroyable on demand rather than waiting for the next purge run.
    try:
        bill = kind.model.objects.trashed().get(id=bill_id, organization=organization)
    except kind.model.DoesNotExist:
        return Response(
            {'error': 'Bill not found in trash'},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Recorded before the purge: afterwards there is no row to read the
    # organisation or the name off.
    record_activity(
        bill, BillActivity.Action.DELETED,
        actor=request.user, organization=organization,
        summary="Deleted permanently",
    )
    purge_bill(bill)
    logger.info(
        "Zoho trash: %s permanently deleted %s %s in org %s",
        request.user.id, kind.slug, bill_id, organization.id,
    )

    return Response(status=status.HTTP_204_NO_CONTENT)


# ============================================================================
# Empty trash (admin only)
# ============================================================================

@extend_schema(
    summary="Empty the Zoho trash",
    description=(
        "Permanently destroys every trashed Zoho bill for the organization, "
        "or just one type when `type` is supplied. Admin only. Cannot be undone."
    ),
    parameters=[OpenApiParameter('type', str, description='Restrict to one document type')],
    tags=['Zoho Trash'],
)
@requires(roles.DELETE_BILLS)
@api_view(['POST'])
@permission_classes([IsAuthenticated, HasOrgPermission])
def trash_empty(request, org_id):
    """Destroy everything currently in this organization's Zoho trash."""
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _org_not_found(org_id)

    if not _is_org_admin(request.user, organization):
        return _forbidden_response()

    kinds, error = _kinds_for(request)
    if error:
        return error

    deleted = {}
    for kind in kinds:
        count = 0
        qs = kind.model.objects.trashed().filter(organization=organization)
        # Per-instance so each uploaded file is removed with its row.
        for bill in qs.iterator():
            try:
                record_activity(
                    bill, BillActivity.Action.DELETED,
                    actor=request.user, organization=organization,
                    summary="Deleted permanently (trash emptied)",
                )
                purge_bill(bill)
                count += 1
            except Exception as exc:
                logger.exception("Zoho trash: failed to purge %s %s: %s", kind.slug, bill.pk, exc)
        deleted[kind.slug] = count

    total = sum(deleted.values())
    logger.info(
        "Zoho trash: %s emptied trash in org %s (%s bills)",
        request.user.id, organization.id, total,
    )

    return Response({
        'message': f'{total} document(s) permanently deleted',
        'deleted_count': total,
        'deleted_by_type': deleted,
    })
