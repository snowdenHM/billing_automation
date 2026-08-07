"""Trash API — list, restore, permanently delete and empty.

Deleting from a bill list moves the bill here (see ``bill_delete_base``).
This module is what the Trash page talks to.

Permissions follow the "safety net stays useful" rule: any org member can
restore, because the person who deleted something by accident is usually
not an admin. Destroying a bill for good is admin-only.
"""

import logging

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.pagination import DefaultPagination
from apps.common.utils import get_organization_from_request
from apps.common.views import generate_signed_bill_file_url
from apps.module.tally.trash import (
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
    """Flatten one trashed bill into the shape the Trash page renders."""
    return {
        'id': str(bill.id),
        'type': kind.slug,
        'type_label': kind.label,
        'list_path': kind.list_path,
        'name': bill.bill_munshi_name,
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
                'message': f"'{requested}' is not a trashable document type.",
                'valid_types': sorted(TRASH_KINDS),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    return [kind], None


# ============================================================================
# List
# ============================================================================

@extend_schema(
    summary="List trashed bills",
    description=(
        "Bills moved to trash and still inside the retention window, newest "
        "deletion first. Expired bills are excluded even if the purge job "
        "has not run yet."
    ),
    parameters=[
        OpenApiParameter('type', str, description='purchase-voucher | journal-entry | payment-voucher | all'),
        OpenApiParameter('search', str, description='Match against the document name'),
    ],
    tags=['Tally Trash'],
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def trash_list(request, org_id):
    """Unified trash listing across every trashable Tally bill type."""
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _org_not_found(org_id)

    kinds, error = _kinds_for(request)
    if error:
        return error

    search = (request.query_params.get('search') or '').strip()

    # The three bill types live in separate tables, so the merge happens in
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
            qs = qs.filter(bill_munshi_name__icontains=search)

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
        'retention_days': summary['retention_days'],
        'counts_by_type': counts,
        'types': [
            {'slug': k.slug, 'label': k.label, 'list_path': k.list_path}
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
    summary="Restore a trashed bill",
    description="Bring a bill back out of the trash. Any org member may restore.",
    tags=['Tally Trash'],
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
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
            {'error': 'Bill not found in trash', 'message': 'It may already have been restored or purged.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    bill.restore()
    logger.info("Trash: restored %s %s in org %s", kind.slug, bill.pk, organization.id)

    return Response({
        'message': f'{kind.label} restored',
        'id': str(bill.id),
        'type': kind.slug,
        'list_path': kind.list_path,
    })


# ============================================================================
# Permanent delete (admin only)
# ============================================================================

@extend_schema(
    summary="Permanently delete a trashed bill",
    description="Destroys the bill row and its uploaded file. Admin only. Cannot be undone.",
    tags=['Tally Trash'],
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
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

    purge_bill(bill)
    logger.info(
        "Trash: %s permanently deleted %s %s in org %s",
        request.user.id, kind.slug, bill_id, organization.id,
    )

    return Response(status=status.HTTP_204_NO_CONTENT)


# ============================================================================
# Empty trash (admin only)
# ============================================================================

@extend_schema(
    summary="Empty the trash",
    description=(
        "Permanently destroys every trashed bill for the organization, or "
        "just one type when `type` is supplied. Admin only. Cannot be undone."
    ),
    parameters=[OpenApiParameter('type', str, description='Restrict to one document type')],
    tags=['Tally Trash'],
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def trash_empty(request, org_id):
    """Destroy everything currently in this organization's trash."""
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
                purge_bill(bill)
                count += 1
            except Exception as exc:
                logger.exception("Trash: failed to purge %s %s: %s", kind.slug, bill.pk, exc)
        deleted[kind.slug] = count

    total = sum(deleted.values())
    logger.info(
        "Trash: %s emptied trash in org %s (%s bills)", request.user.id, organization.id, total
    )

    return Response({
        'message': f'{total} document(s) permanently deleted',
        'deleted_count': total,
        'deleted_by_type': deleted,
    })
