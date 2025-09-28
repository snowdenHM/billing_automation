from django.urls import path
from .views import (
    organization_list_view,
    organization_detail_view,
    organization_update_view,
    organization_add_member_view,
    organization_members_view,
    organization_remove_member_view,
    organization_update_member_role_view,
    organization_issue_api_key_view,
    organization_list_api_keys_view,
    organization_modules_view,
    modules_catalog_view,
    membership_list_view,
    membership_detail_view,
    membership_update_view,
    membership_delete_view,
    organization_onboarding_create_view,
    organization_onboarding_enable_module_view,
)
from .class_views import (
    OrganizationRevokeAPIKeyView,
    OrganizationSetModuleView,
)

app_name = "organizations"

urlpatterns = [
    # Organization endpoints
    path("org/", organization_list_view, name="organization-list"),
    path("org/<uuid:pk>/", organization_detail_view, name="organization-detail"),
    path("org/<uuid:pk>/update/", organization_update_view, name="organization-update"),

    # Member management endpoints
    path("org/<uuid:org_id>/members/", organization_members_view, name="org-members"),
    path("org/<uuid:org_id>/members/add/", organization_add_member_view, name="org-add-member"),
    path("org/<uuid:org_id>/members/<uuid:membership_id>/", organization_remove_member_view, name="org-remove-member"),
    path("org/<uuid:org_id>/members/<uuid:membership_id>/role/", organization_update_member_role_view, name="org-update-member-role"),

    # API Key management endpoints
    path("org/<uuid:org_id>/api-keys/", organization_list_api_keys_view, name="org-list-api-keys"),
    path("org/<uuid:org_id>/api-keys/issue/", organization_issue_api_key_view, name="org-issue-api-key"),
    path("org/<uuid:org_id>/api-keys/<uuid:key_id>/revoke/", OrganizationRevokeAPIKeyView.as_view(), name="org-revoke-api-key"),

    # Module management endpoints
    path("org/<uuid:org_id>/modules/", organization_modules_view, name="org-modules"),
    path("org/<uuid:org_id>/modules/<str:code>/", OrganizationSetModuleView.as_view(), name="org-set-module"),
    path("modules/catalog/", modules_catalog_view, name="modules-catalog"),

    # Membership endpoints
    path("memberships/", membership_list_view, name="membership-list"),
    path("memberships/<uuid:pk>/", membership_detail_view, name="membership-detail"),
    path("memberships/<uuid:pk>/update/", membership_update_view, name="membership-update"),
    path("memberships/<uuid:pk>/delete/", membership_delete_view, name="membership-delete"),

    # Onboarding endpoints
    path("org/onboarding/create/", organization_onboarding_create_view, name="org-onboarding-create"),
    path("org/onboarding/<uuid:org_id>/enable-module/", organization_onboarding_enable_module_view, name="org-onboarding-enable-module"),
]
