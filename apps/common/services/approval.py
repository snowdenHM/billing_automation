"""The approval step between verifying a bill and posting it.

An Accountant can now reach the data-entry endpoints (see
``apps.common.roles``), which raises the question the roles alone do not
answer: who checks the work before it lands in the books. This is that
check. A verified bill is submitted, a Manager or Admin approves or
sends it back, and only an approved bill may sync.

The gate is per organisation and off by default. Where
``Organization.require_bill_approval`` is False -- which is every
organisation until somebody turns it on -- :func:`gate_sync` returns
None and sync behaves exactly as it did before this module existed.

Unlike the activity log, these calls are *not* best-effort. A swallowed
failure here would be a bill that nobody approved looking approved, so
the write errors are allowed to surface.
"""

import logging

from django.db import transaction
from django.utils import timezone

from apps.common.models import BillActivity, BillApproval
from apps.common.services.activity import record_activity, resolve_bill_kind

logger = logging.getLogger(__name__)


class ApprovalError(Exception):
    """A submit or decision that the current state does not allow."""

    def __init__(self, message, code):
        super().__init__(message)
        self.message = message
        self.code = code


def approval_required(organization):
    return bool(getattr(organization, "require_bill_approval", False))


def approval_for(bill):
    """The decision row for *bill*, or None."""
    module, bill_type = resolve_bill_kind(bill)
    if module is None:
        return None
    return BillApproval.objects.filter(
        module=module, bill_type=bill_type, bill_id=bill.pk,
    ).select_related("submitted_by", "decided_by").first()


def state_of(bill):
    """``pending`` / ``approved`` / ``rejected``, or None when never submitted."""
    approval = approval_for(bill)
    return approval.state if approval else None


def gate_sync(bill, organization=None):
    """Why *bill* may not be posted, or None when it may.

    Returns ``(message, code)``. Callers turn that into their own error
    response, since the three modules word theirs differently.
    """
    org = organization or getattr(bill, "organization", None)
    if not approval_required(org):
        return None

    approval = approval_for(bill)
    if approval is None:
        return ("This bill has not been submitted for approval. "
                "Submit it, and a Manager can approve it for posting.",
                "APPROVAL_NOT_SUBMITTED")
    if approval.state == BillApproval.State.APPROVED:
        return None
    if approval.state == BillApproval.State.REJECTED:
        note = approval.decided_note or "no reason given"
        return (f"This bill was sent back: {note}. "
                "Correct it and submit it again.",
                "APPROVAL_REJECTED")
    return ("This bill is waiting for approval. A Manager or Admin must "
            "approve it before it can be posted.",
            "APPROVAL_PENDING")


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

def submit(bill, *, actor, note="", organization=None):
    """Put *bill* in the approval queue. Returns the BillApproval.

    Submitting an already-approved bill is refused rather than silently
    reopening it: an approval that a later submit can erase is not an
    approval. A rejected bill reuses its row, which is the normal
    correct-and-resubmit path.
    """
    module, bill_type = resolve_bill_kind(bill)
    if module is None:
        raise ApprovalError("This kind of bill cannot be submitted for approval.",
                            "APPROVAL_UNSUPPORTED_BILL")

    org = organization or getattr(bill, "organization", None)
    if org is None:
        raise ApprovalError("Bill has no organisation.", "APPROVAL_NO_ORG")

    with transaction.atomic():
        approval = (
            BillApproval.objects
            .select_for_update()
            .filter(module=module, bill_type=bill_type, bill_id=bill.pk)
            .first()
        )
        if approval and approval.state == BillApproval.State.APPROVED:
            raise ApprovalError("This bill is already approved.",
                                "APPROVAL_ALREADY_APPROVED")
        if approval and approval.state == BillApproval.State.PENDING:
            raise ApprovalError("This bill is already waiting for approval.",
                                "APPROVAL_ALREADY_PENDING")

        if approval is None:
            approval = BillApproval(
                organization=org, module=module,
                bill_type=bill_type, bill_id=bill.pk,
            )
        approval.state = BillApproval.State.PENDING
        approval.submitted_by = actor if getattr(actor, "is_authenticated", False) else None
        approval.submitted_at = timezone.now()
        approval.submitted_note = (note or "")[:500]
        # Clear the previous decision, so a resubmitted bill does not
        # show last week's rejection beside this week's request.
        approval.decided_by = None
        approval.decided_at = None
        approval.decided_note = ""
        approval.save()

    record_activity(
        bill, BillActivity.Action.APPROVAL_SUBMITTED,
        actor=actor, organization=org,
        summary=(note or "Submitted for approval")[:255],
    )
    return approval


def decide(bill, *, approved, actor, note="", organization=None):
    """Approve or send back a pending bill. Returns the BillApproval."""
    module, bill_type = resolve_bill_kind(bill)
    if module is None:
        raise ApprovalError("This kind of bill cannot be approved.",
                            "APPROVAL_UNSUPPORTED_BILL")

    org = organization or getattr(bill, "organization", None)

    with transaction.atomic():
        approval = (
            BillApproval.objects
            .select_for_update()
            .filter(module=module, bill_type=bill_type, bill_id=bill.pk)
            .first()
        )
        if approval is None:
            raise ApprovalError("This bill has not been submitted for approval.",
                                "APPROVAL_NOT_SUBMITTED")
        if approval.state != BillApproval.State.PENDING:
            raise ApprovalError(
                f"This bill is already {approval.get_state_display().lower()}.",
                "APPROVAL_ALREADY_DECIDED",
            )
        # Whoever asked for the check should not be the one who signs it
        # off. The point of the step is a second pair of eyes.
        submitter_id = approval.submitted_by_id
        if submitter_id and actor is not None and submitter_id == getattr(actor, "pk", None):
            raise ApprovalError(
                "You submitted this bill, so somebody else has to approve it.",
                "APPROVAL_SELF",
            )

        approval.state = (BillApproval.State.APPROVED if approved
                          else BillApproval.State.REJECTED)
        approval.decided_by = actor if getattr(actor, "is_authenticated", False) else None
        approval.decided_at = timezone.now()
        approval.decided_note = (note or "")[:500]
        approval.save(update_fields=["state", "decided_by", "decided_at",
                                     "decided_note", "updated_at"])

    record_activity(
        bill,
        BillActivity.Action.APPROVED if approved else BillActivity.Action.REJECTED,
        actor=actor, organization=org,
        summary=(note or ("Approved" if approved else "Sent back"))[:255],
    )
    return approval


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def pending_queue(organization, *, module=None, bill_type=None):
    """Everything waiting for a decision in this organisation, oldest first.

    Oldest first on purpose: a queue is worked through, and the bill
    that has waited longest is the one holding somebody up.
    """
    qs = BillApproval.objects.filter(
        organization=organization, state=BillApproval.State.PENDING,
    ).select_related("submitted_by")
    if module:
        qs = qs.filter(module=module)
    if bill_type:
        qs = qs.filter(bill_type=bill_type)
    return qs.order_by("submitted_at")


def states_for(bills):
    """``{bill_id: state}`` for a page of bills, in one query.

    A list view needs the badge for every row; without this it would be
    one lookup per bill.
    """
    ids = [b.pk for b in bills]
    if not ids:
        return {}
    rows = BillApproval.objects.filter(bill_id__in=ids).values_list("bill_id", "state")
    return {str(bill_id): state for bill_id, state in rows}


def as_dict(approval):
    if approval is None:
        return None
    return {
        "state": approval.state,
        "state_display": approval.get_state_display(),
        "submitted_by": _name(approval.submitted_by),
        "submitted_at": approval.submitted_at,
        "submitted_note": approval.submitted_note,
        "decided_by": _name(approval.decided_by),
        "decided_at": approval.decided_at,
        "decided_note": approval.decided_note,
    }


def _name(user):
    if user is None:
        return ""
    return (getattr(user, "get_full_name", lambda: "")() or "").strip() or getattr(user, "email", "")
