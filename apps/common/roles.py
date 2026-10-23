"""What each role in an organisation is allowed to do.

The four roles have existed on the membership record from the start, but
nothing read them: thirty-four endpoints ask for ``IsOrgAdmin``, which
means the ADMIN role and nothing else. An Accountant cannot upload a
bill, a Manager cannot verify one, and the only way to let somebody work
is to make them an Admin — which is why, in practice, everyone is one.

That also blocks the approval workflow outright. "Accountant submits to
Manager" cannot exist while an Accountant has no rights at all.

The matrix below is deliberately additive: ADMIN keeps everything it
could already do, and the other roles gain what their job needs. Nobody
loses access when this ships, which is the only safe direction for a
product people are using today.
"""

from apps.organizations.models import OrgMembership

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
# Named for what a person does, not for the endpoint that does it, so one
# action can cover several routes and a new route does not invent a new
# permission.

VIEW_BILLS = "view_bills"
UPLOAD_BILLS = "upload_bills"
ANALYSE_BILLS = "analyse_bills"
EDIT_BILLS = "edit_bills"
VERIFY_BILLS = "verify_bills"
APPROVE_BILLS = "approve_bills"          # the Manager half of the workflow
SYNC_BILLS = "sync_bills"                # posting to Tally or Zoho
MOVE_BILLS = "move_bills"
TRASH_BILLS = "trash_bills"
DELETE_BILLS = "delete_bills"            # permanent, past the trash
EXPORT_REPORTS = "export_reports"
MANAGE_MASTERS = "manage_masters"        # ledgers, stock items
MANAGE_SETTINGS = "manage_settings"      # Tally config, API keys, integrations
MANAGE_MEMBERS = "manage_members"

ALL_ACTIONS = (
    VIEW_BILLS, UPLOAD_BILLS, ANALYSE_BILLS, EDIT_BILLS, VERIFY_BILLS,
    APPROVE_BILLS, SYNC_BILLS, MOVE_BILLS, TRASH_BILLS, DELETE_BILLS,
    EXPORT_REPORTS, MANAGE_MASTERS, MANAGE_SETTINGS, MANAGE_MEMBERS,
)

# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------
# ACCOUNTANT does the data entry: everything up to and including verify,
# but not posting to Tally — that is what the approval step is for — and
# not permanent deletion, which is not recoverable.
#
# MANAGER reviews that work: the same, plus approve, plus sync and
# permanent deletion. Not members or integration settings, which are the
# account owner's business.
#
# CONSULTANT is the outside accountant: read and export, change nothing.

ROLE_PERMISSIONS = {
    OrgMembership.ADMIN: set(ALL_ACTIONS),
    OrgMembership.MANAGER: {
        VIEW_BILLS, UPLOAD_BILLS, ANALYSE_BILLS, EDIT_BILLS, VERIFY_BILLS,
        APPROVE_BILLS, SYNC_BILLS, MOVE_BILLS, TRASH_BILLS, DELETE_BILLS,
        EXPORT_REPORTS, MANAGE_MASTERS,
    },
    OrgMembership.ACCOUNTANT: {
        VIEW_BILLS, UPLOAD_BILLS, ANALYSE_BILLS, EDIT_BILLS, VERIFY_BILLS,
        MOVE_BILLS, TRASH_BILLS, EXPORT_REPORTS,
    },
    OrgMembership.CONSULTANT: {
        VIEW_BILLS, EXPORT_REPORTS,
    },
}


def permissions_for(role):
    """Everything *role* may do. An unknown role gets nothing."""
    return ROLE_PERMISSIONS.get(role, set())


def membership_role(user, organization_id):
    """The user's active role in that organisation, or None.

    Staff and superusers are not given a role here. Callers decide
    whether to let them past, so the two questions — "what is this
    person's role" and "is this person support staff" — stay separate.
    """
    if not user or not getattr(user, "is_authenticated", False) or not organization_id:
        return None
    membership = (
        OrgMembership.objects
        .filter(user=user, organization_id=organization_id, is_active=True)
        .only("role")
        .first()
    )
    return membership.role if membership else None


def can(user, organization_id, action):
    """May *user* do *action* in that organisation?"""
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    return action in permissions_for(membership_role(user, organization_id))
