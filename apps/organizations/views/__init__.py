# apps/organizations/views/__init__.py
"""
Organization views – split into logical modules for maintainability.
All public view functions are re-exported here so existing imports
like ``from apps.organizations.views import ...`` keep working.
"""

from .organization import (
    organization_list_view,
    organization_detail_view,
    organization_update_view,
)

from .membership import (
    organization_add_member_view,
    organization_invite_user_view,
    organization_members_view,
    organization_remove_member_view,
    organization_delete_member_view,
    organization_update_member_role_view,
    membership_list_view,
    membership_detail_view,
    membership_update_view,
    membership_delete_view,
)

from .api_keys import (
    organization_issue_api_key_view,
    organization_list_api_keys_view,
    organization_revoke_api_key_view,
)

from .modules import (
    organization_modules_view,
    organization_set_module_view,
    modules_catalog_view,
)

from .onboarding import (
    organization_create_with_module_view,
    organization_onboarding_create_view,
    organization_onboarding_enable_module_view,
)

__all__ = [
    # Organization CRUD
    "organization_list_view",
    "organization_detail_view",
    "organization_update_view",
    # Membership
    "organization_add_member_view",
    "organization_invite_user_view",
    "organization_members_view",
    "organization_remove_member_view",
    "organization_delete_member_view",
    "organization_update_member_role_view",
    "membership_list_view",
    "membership_detail_view",
    "membership_update_view",
    "membership_delete_view",
    # API Keys
    "organization_issue_api_key_view",
    "organization_list_api_keys_view",
    "organization_revoke_api_key_view",
    # Modules
    "organization_modules_view",
    "organization_set_module_view",
    "modules_catalog_view",
    # Onboarding
    "organization_create_with_module_view",
    "organization_onboarding_create_view",
    "organization_onboarding_enable_module_view",
]
