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


# ---------------------------------------------------------------------------
# Saying all this to a person
# ---------------------------------------------------------------------------
# Picking a role for a new member used to be four buttons labelled Admin,
# Manager, Accountant and Consultant, with nothing to say what any of
# them could do. The matrix above was the only answer and it lived in a
# Python file, so the person choosing had to guess — and the frontend
# kept its own hardcoded copy of the role list, which is how a screen
# and its enforcement drift apart without anyone noticing.
#
# The labels below exist so the matrix can be shown rather than
# guessed, and so the screen reads them from the server instead of
# repeating them.

ROLE_LABELS = {
    OrgMembership.ADMIN: "Admin",
    OrgMembership.MANAGER: "Manager",
    OrgMembership.ACCOUNTANT: "Accountant",
    OrgMembership.CONSULTANT: "Consultant",
}

#: One line, in the words of somebody deciding who to invite.
ROLE_SUMMARIES = {
    OrgMembership.ADMIN:
        "Runs the workspace. Everything a Manager can do, plus inviting "
        "people and changing the Tally and Zoho settings.",
    OrgMembership.MANAGER:
        "Reviews and posts. Can do all the bill work, approve what the "
        "Accountant submits, and post to Tally or Zoho. Cannot invite "
        "people or change integration settings.",
    OrgMembership.ACCOUNTANT:
        "Does the data entry. Uploads, reads and verifies bills, but "
        "cannot post them — a Manager approves and posts.",
    OrgMembership.CONSULTANT:
        "Looks, does not touch. Reads every bill and exports reports. "
        "Changes nothing. Meant for an outside accountant.",
}

#: What each action means on screen, and which heading it sits under.
ACTION_LABELS = {
    VIEW_BILLS: "See bills",
    UPLOAD_BILLS: "Upload bills",
    ANALYSE_BILLS: "Run analysis",
    EDIT_BILLS: "Edit bill details",
    VERIFY_BILLS: "Verify bills",
    APPROVE_BILLS: "Approve submitted bills",
    SYNC_BILLS: "Post to Tally / Zoho",
    MOVE_BILLS: "Move between voucher types",
    TRASH_BILLS: "Move to Trash",
    DELETE_BILLS: "Delete permanently",
    EXPORT_REPORTS: "Download reports",
    MANAGE_MASTERS: "Manage ledgers and stock items",
    MANAGE_SETTINGS: "Change Tally / Zoho settings and API keys",
    MANAGE_MEMBERS: "Invite and remove members",
}

ACTION_GROUPS = {
    VIEW_BILLS: "Bills",
    UPLOAD_BILLS: "Bills",
    ANALYSE_BILLS: "Bills",
    EDIT_BILLS: "Bills",
    VERIFY_BILLS: "Bills",
    APPROVE_BILLS: "Posting",
    SYNC_BILLS: "Posting",
    MOVE_BILLS: "Bills",
    TRASH_BILLS: "Bills",
    DELETE_BILLS: "Bills",
    EXPORT_REPORTS: "Reports",
    MANAGE_MASTERS: "Administration",
    MANAGE_SETTINGS: "Administration",
    MANAGE_MEMBERS: "Administration",
}

#: The order a reader should meet the actions in, roughly the order a
#: bill passes through them.
GROUP_ORDER = ("Bills", "Posting", "Reports", "Administration")


def describe_roles():
    """The whole matrix, shaped for a screen rather than for a check.

    One source of truth: the screen that asks somebody to choose a role
    renders this, so what it promises and what the server enforces
    cannot disagree.
    """
    return {
        "roles": [
            {
                "value": role,
                "label": ROLE_LABELS.get(role, role.title()),
                "summary": ROLE_SUMMARIES.get(role, ""),
                "permissions": sorted(permissions_for(role)),
            }
            for role in (OrgMembership.ADMIN, OrgMembership.MANAGER,
                         OrgMembership.ACCOUNTANT, OrgMembership.CONSULTANT)
        ],
        "actions": [
            {
                "value": action,
                "label": ACTION_LABELS.get(action, action.replace("_", " ").capitalize()),
                "group": ACTION_GROUPS.get(action, "Other"),
                "allowed_for": sorted(
                    role for role, allowed in ROLE_PERMISSIONS.items()
                    if action in allowed
                ),
            }
            for action in ALL_ACTIONS
        ],
        "group_order": list(GROUP_ORDER),
    }
