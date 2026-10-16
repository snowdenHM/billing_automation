"""Organization provisioning shared by signup and the onboarding views.

Signup collects an organisation name, GSTIN and accounting software, but
until now it only stored those as plain text on the User row
(``signup_organization_name`` and friends). No Organization was created
and no membership was granted, so a freshly registered user landed in an
account with an empty Clients list and nowhere to upload a bill — the
Clients page lists organisations the user has an *active membership* in
(see ``organization_list_view``).

``provision_organization`` creates the same three rows the onboarding
flow does — Organization, OrgMembership(ADMIN), OrganizationModule — so
both entry points produce an identical workspace.
"""

import logging

from django.db import transaction
from django.utils.text import slugify

from .models import Module, Organization, OrganizationModule, OrgMembership

logger = logging.getLogger(__name__)


# The signup form's "Accounting software" values are not the Module codes.
# Mapped here rather than at the call site so there is one place to fix
# when a module is added.
SOFTWARE_TO_MODULE_CODE = {
    "tally": "tally",
    "zoho_books": "zoho",
    "zoho": "zoho",
}


def _unique_name_and_slug(desired_name):
    """Return a ``(name, slug)`` pair no existing Organization is using.

    ``Organization.name`` and ``.slug`` are both unique columns, so two
    companies that genuinely share a name would collide on the second
    signup.

    The important part is what this does NOT do: it never reuses an
    existing Organization just because the name matches. Doing so would
    let anyone type a known company name at signup and be granted admin
    membership of that company's real workspace. A colliding name always
    produces a NEW, separate organisation; the operator can rename it
    afterwards in settings.
    """
    base_name = (desired_name or "").strip()[:255] or "My Organization"
    base_slug = slugify(base_name)[:200] or "organization"

    name, slug = base_name, base_slug
    suffix = 2
    while (
        Organization.objects.filter(name=name).exists()
        or Organization.objects.filter(slug=slug).exists()
    ):
        tag = f" ({suffix})"
        name = f"{base_name[:255 - len(tag)]}{tag}"
        slug = f"{base_slug[:200 - len(str(suffix)) - 1]}-{suffix}"
        suffix += 1
        if suffix > 50:
            # Pathological case; fall back to the random unique_name style
            # rather than spinning.
            from .models import generate_unique_org_name

            token = generate_unique_org_name()
            name, slug = f"{base_name[:240]} {token}", slugify(f"{base_slug[:190]}-{token}")
            break

    return name, slug


def _enable_module(organization, accounting_software):
    """Enable the module matching the signup's accounting-software choice.

    Best effort: the Module rows are seeded by hand in admin, so a missing
    row must not take the signup down with it. Without a module the
    workspace still exists and a module can be enabled later from
    onboarding.
    """
    code = SOFTWARE_TO_MODULE_CODE.get((accounting_software or "").strip().lower())
    if not code:
        return None

    module = Module.objects.filter(code=code).first()
    if module is None:
        logger.warning(
            "Signup for org %s asked for module %r but no such Module row "
            "exists — organisation created without it.",
            organization.id, code,
        )
        return None

    org_module, _ = OrganizationModule.objects.get_or_create(
        organization=organization, module=module, defaults={"is_active": True},
    )
    return org_module


@transaction.atomic
def provision_organization(user, *, name, gst_number="", accounting_software=""):
    """Create an organisation owned by *user*, with them as its admin.

    Returns the Organization, or ``None`` when no name was supplied.

    Atomic so a half-built workspace — an organisation nobody is a member
    of, say — can never be committed.
    """
    if not (name or "").strip():
        return None

    final_name, final_slug = _unique_name_and_slug(name)
    gst = (gst_number or "").strip().upper()

    organization = Organization.objects.create(
        name=final_name,
        slug=final_slug,
        gst_number=gst or None,
        owner=user,
        created_by=user,
        status=Organization.ACTIVE,
    )

    # The creator is the admin of their own workspace. Without this row the
    # organisation exists but is invisible: every org-scoped view filters on
    # active membership.
    OrgMembership.objects.create(
        organization=organization,
        user=user,
        role=OrgMembership.ADMIN,
        is_active=True,
    )

    _enable_module(organization, accounting_software)

    if final_name != (name or "").strip():
        logger.info(
            "Organisation name %r was taken; created %r for user %s instead.",
            name, final_name, user.pk,
        )

    return organization
