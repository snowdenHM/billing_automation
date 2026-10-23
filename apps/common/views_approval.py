"""Submit a bill for approval, decide on it, and read the queue.

One set of routes for every module. A bill id is a UUID and unique
across all six bill models, so the caller sends the id and the resolver
below finds the row -- the frontend does not have to know which table a
bill lives in to ask for it to be approved.
"""

import logging

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common import roles
from apps.common.models import BillApproval
from apps.common.pagination import DefaultPagination
from apps.common.permissions import HasOrgPermission, requires
from apps.common.services import approval as approval_service
from apps.common.utils import get_organization_from_request

logger = logging.getLogger(__name__)


def _bill_models():
    """The six bill models, imported late to stay clear of app loading."""
    from apps.module.tally.models import (
        TallyExpenseBill, TallyPaymentBill, TallyReceiptBill, TallySalesBill,
        TallyVendorBill,
    )
    from apps.module.zoho.models import ExpenseBill, JournalBill, VendorBill

    return (TallyVendorBill, TallyExpenseBill, TallyPaymentBill, TallySalesBill,
            TallyReceiptBill, VendorBill, ExpenseBill, JournalBill)


def find_bill(bill_id, organization):
    """The bill with that id inside *organization*, or None.

    Scoped to the organisation in the query rather than checked
    afterwards, so a id from another tenant simply is not found.
    """
    for model in _bill_models():
        manager = getattr(model, "objects")
        qs = manager.alive() if hasattr(manager, "alive") else manager.all()
        bill = qs.filter(id=bill_id, organization=organization).first()
        if bill is not None:
            return bill
    return None


class ApprovalSerializer(serializers.ModelSerializer):
    state_label = serializers.CharField(source="get_state_display", read_only=True)
    bill_type_label = serializers.CharField(source="get_bill_type_display", read_only=True)
    submitted_by_email = serializers.EmailField(
        source="submitted_by.email", read_only=True, default=None)
    decided_by_email = serializers.EmailField(
        source="decided_by.email", read_only=True, default=None)

    class Meta:
        model = BillApproval
        fields = [
            "id", "module", "bill_type", "bill_type_label", "bill_id",
            "state", "state_label",
            "submitted_by_email", "submitted_at", "submitted_note",
            "decided_by_email", "decided_at", "decided_note",
        ]
        read_only_fields = fields


class _NoteSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)


def _error(exc):
    return Response(
        {"error": "Approval", "message": exc.message, "error_code": exc.code},
        status=status.HTTP_409_CONFLICT,
    )


def _not_found(bill_id):
    return Response(
        {"error": "Not Found",
         "message": f"No bill with id {bill_id} in this organisation.",
         "error_code": "BILL_NOT_FOUND"},
        status=status.HTTP_404_NOT_FOUND,
    )


@extend_schema(
    request=_NoteSerializer,
    responses={200: ApprovalSerializer},
    tags=["Approvals"],
)
@requires(roles.VERIFY_BILLS)
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def submit_for_approval(request, org_id, bill_id):
    """Put a bill in the approval queue.

    Gated on VERIFY_BILLS rather than a right of its own: the person who
    finished the bill is the person who hands it on, and an Accountant
    who can verify can submit.
    """
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _not_found(bill_id)

    bill = find_bill(bill_id, organization)
    if bill is None:
        return _not_found(bill_id)

    note = _NoteSerializer(data=request.data)
    note.is_valid(raise_exception=True)

    try:
        approval = approval_service.submit(
            bill, actor=request.user,
            note=note.validated_data.get("note", ""),
            organization=organization,
        )
    except approval_service.ApprovalError as exc:
        return _error(exc)

    return Response(ApprovalSerializer(approval).data)


@extend_schema(
    request=_NoteSerializer,
    responses={200: ApprovalSerializer},
    tags=["Approvals"],
)
@requires(roles.APPROVE_BILLS)
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def approve_bill(request, org_id, bill_id):
    return _decide(request, org_id, bill_id, approved=True)


@extend_schema(
    request=_NoteSerializer,
    responses={200: ApprovalSerializer},
    tags=["Approvals"],
)
@requires(roles.APPROVE_BILLS)
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def reject_bill(request, org_id, bill_id):
    return _decide(request, org_id, bill_id, approved=False)


def _decide(request, org_id, bill_id, *, approved):
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _not_found(bill_id)

    bill = find_bill(bill_id, organization)
    if bill is None:
        return _not_found(bill_id)

    note = _NoteSerializer(data=request.data)
    note.is_valid(raise_exception=True)
    text = note.validated_data.get("note", "")

    # A bill sent back without a reason gives the accountant nothing to
    # act on, and the bill comes straight back.
    if not approved and not text.strip():
        return Response(
            {"error": "Approval",
             "message": "Say why the bill is being sent back, so it can be corrected.",
             "error_code": "APPROVAL_REASON_REQUIRED"},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    try:
        approval = approval_service.decide(
            bill, approved=approved, actor=request.user,
            note=text, organization=organization,
        )
    except approval_service.ApprovalError as exc:
        return _error(exc)

    return Response(ApprovalSerializer(approval).data)


@extend_schema(
    parameters=[
        OpenApiParameter("module", str, description="tally or zoho"),
        OpenApiParameter("bill_type", str, description="vendor, expense, payment or journal"),
    ],
    responses={200: ApprovalSerializer(many=True)},
    tags=["Approvals"],
)
@requires(roles.VIEW_BILLS)
@api_view(["GET"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def approval_queue(request, org_id):
    """Bills waiting for a decision, oldest first."""
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response(
            {"error": "Not Found", "message": "Organisation not found.",
             "error_code": "ORG_NOT_FOUND"},
            status=status.HTTP_404_NOT_FOUND,
        )

    queryset = approval_service.pending_queue(
        organization,
        module=request.query_params.get("module"),
        bill_type=request.query_params.get("bill_type"),
    )

    paginator = DefaultPagination()
    page = paginator.paginate_queryset(queryset, request)
    data = ApprovalSerializer(page, many=True).data
    response = paginator.get_paginated_response(data)
    response.data["approval_required"] = approval_service.approval_required(organization)
    return response


@extend_schema(responses={200: ApprovalSerializer}, tags=["Approvals"])
@requires(roles.VIEW_BILLS)
@api_view(["GET"])
@permission_classes([IsAuthenticated, HasOrgPermission])
def approval_detail(request, org_id, bill_id):
    """One bill's approval state, or nulls when it was never submitted."""
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return _not_found(bill_id)

    bill = find_bill(bill_id, organization)
    if bill is None:
        return _not_found(bill_id)

    approval = approval_service.approval_for(bill)
    return Response({
        "approval_required": approval_service.approval_required(organization),
        "can_approve": roles.can(request.user, organization.id, roles.APPROVE_BILLS),
        "approval": ApprovalSerializer(approval).data if approval else None,
    })
