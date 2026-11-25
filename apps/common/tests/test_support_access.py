"""Consent-gated support access — the guards, not the happy path.

Each test below stands for one sentence in docs/support-desk/SPEC.md that
is only true if something refuses:

    Access must be asked for        -> only a client ADMIN mints, and an
                                       agent cannot mint their own consent
    Access must be current          -> an expired grant fails auth even
                                       with status still "active"
    Access ends with its reason     -> closing the ticket revokes
    The client can change their mind-> revoke kills auth on the next request
    Consent cannot be laundered     -> the forbidden list holds at any
                                       scope, and a second agent cannot
                                       redeem a grant already spoken for
    Nothing is anonymous            -> one event per impersonated request

The code's plaintext is checked for twice over: that it comes back once,
and that it is nowhere in the row that remembers the grant.
"""
import hashlib

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.organizations.models import Organization, OrgMembership
from apps.support import access_tokens
from apps.support.models import (
    SupportAccessEvent,
    SupportAccessGrant,
    SupportAccessSession,
    SupportAgent,
    SupportTicket,
)
from django.contrib.auth import get_user_model

User = get_user_model()

PASSWORD = "x-test-pass-123"

# A locmem cache, per test: the redemption lockout is cache-backed, and a
# shared Redis would carry one test's failed attempts into the next.
LOCAL_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "support-access-tests",
    }
}


@override_settings(CACHES=LOCAL_CACHE)
class SupportAccessTestCase(APITestCase):
    """Shared cast: one client org (admin + plain member) and one agent."""

    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(email="admin@client.test", password=PASSWORD)
        self.member = User.objects.create_user(email="member@client.test", password=PASSWORD)
        self.org = Organization.objects.create(
            name="Client Org", owner=self.admin, created_by=self.admin
        )
        OrgMembership.objects.create(
            organization=self.org, user=self.admin, role=OrgMembership.ADMIN
        )
        OrgMembership.objects.create(
            organization=self.org, user=self.member, role=OrgMembership.MANAGER
        )

        self.agent_user = User.objects.create_user(
            email="agent@billmunshi.test", password=PASSWORD, is_staff=True
        )
        self.agent = SupportAgent.objects.create(
            user=self.agent_user, display_name="Asha (support)"
        )

        self.ticket = SupportTicket.objects.create(
            organization=self.org,
            user=self.admin,
            subject="Sync is stuck",
            message="Tally sync has been queued for an hour.",
        )

    # -- helpers ---------------------------------------------------------

    def mint_url(self, ticket=None):
        return reverse(
            "support:access-grant-mint", kwargs={"ticket_id": (ticket or self.ticket).pk}
        )

    def mint(self, scope=SupportAccessGrant.SCOPE_FULL, ttl_minutes=60, as_user=None):
        self.client.force_authenticate(user=as_user or self.admin)
        response = self.client.post(
            self.mint_url(), {"scope": scope, "ttl_minutes": ttl_minutes}, format="json"
        )
        self.client.force_authenticate(user=None)
        return response

    def redeem(self, code, agent_user=None):
        self.client.force_authenticate(user=agent_user or self.agent_user)
        response = self.client.post(
            reverse("support_access:desk-access-redeem"), {"code": code}, format="json"
        )
        self.client.force_authenticate(user=None)
        return response

    def live_session(self, scope=SupportAccessGrant.SCOPE_FULL):
        """Mint, redeem, and come back with the impersonation token."""
        minted = self.mint(scope=scope)
        self.assertEqual(minted.status_code, status.HTTP_201_CREATED, minted.data)
        redeemed = self.redeem(minted.data["code"])
        self.assertEqual(redeemed.status_code, status.HTTP_200_OK, redeemed.data)
        grant = SupportAccessGrant.objects.get(pk=minted.data["id"])
        session = SupportAccessSession.objects.get(pk=redeemed.data["session"]["id"])
        return redeemed.data["token"], grant, session

    def as_impersonator(self, token):
        self.client.force_authenticate(user=None)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")


class MintingIsTheClientsAloneTests(SupportAccessTestCase):
    """No support endpoint creates a grant, and no agent mints one."""

    def test_support_agent_cannot_mint_a_grant_for_themselves(self):
        # The strongest form of the attack: the agent has somehow also
        # become an ADMIN member of the client organization (a shared demo
        # account, a colleague's own org). The ADMIN role is then real, and
        # the refusal has to come from them being an agent.
        OrgMembership.objects.create(
            organization=self.org, user=self.agent_user, role=OrgMembership.ADMIN
        )
        response = self.mint(as_user=self.agent_user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(SupportAccessGrant.objects.exists())

    def test_staff_flag_alone_does_not_pass_the_admin_check(self):
        # ``assert_org_access`` elsewhere in the project lets staff through.
        # Here that shortcut would be the whole hole.
        staff_outsider = User.objects.create_user(
            email="staff@billmunshi.test", password=PASSWORD, is_staff=True
        )
        response = self.mint(as_user=staff_outsider)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(SupportAccessGrant.objects.exists())

    def test_non_admin_member_cannot_mint(self):
        response = self.mint(as_user=self.member)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(SupportAccessGrant.objects.exists())

    def test_admin_of_another_org_cannot_mint_on_this_ticket(self):
        outsider = User.objects.create_user(email="other@client.test", password=PASSWORD)
        other_org = Organization.objects.create(
            name="Other Org", owner=outsider, created_by=outsider
        )
        OrgMembership.objects.create(
            organization=other_org, user=outsider, role=OrgMembership.ADMIN
        )
        response = self.mint(as_user=outsider)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_ttl_is_capped_at_eight_hours(self):
        response = self.mint(ttl_minutes=60 * 24 * 7)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ThePlaintextCodeTests(SupportAccessTestCase):
    """Returned once, stored never."""

    def test_code_is_returned_once_and_never_stored(self):
        minted = self.mint()
        self.assertEqual(minted.status_code, status.HTTP_201_CREATED)
        code = minted.data["code"]

        grant = SupportAccessGrant.objects.get(pk=minted.data["id"])
        digest = hashlib.sha256(code.encode()).hexdigest()
        self.assertEqual(grant.token_hash, digest)

        # Nothing on the row carries the code or any part of it past the
        # label, which is the prefix plus the first group.
        stored = " ".join(
            str(value) for value in SupportAccessGrant.objects.filter(pk=grant.pk).values()[0].values()
        )
        self.assertNotIn(code, stored)
        secret_tail = code.split("-", 2)[2]
        self.assertNotIn(secret_tail, stored)
        self.assertEqual(grant.code_label, "-".join(code.split("-")[:2]))

        # And a second look at the grant does not hand it back.
        self.client.force_authenticate(user=self.admin)
        listed = self.client.get(
            reverse("support_access:org-access-grants", kwargs={"org_id": self.org.pk})
        )
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertNotIn("code", listed.data["data"][0])
        self.assertNotIn("token_hash", listed.data["data"][0])
        self.assertNotIn(code, str(listed.data))

    def test_code_shape_and_alphabet(self):
        code = self.mint().data["code"]
        prefix, *groups = code.split("-")
        self.assertEqual(prefix, "BM")
        self.assertEqual([len(g) for g in groups], [4, 4, 4])
        for char in "".join(groups):
            self.assertIn(char, access_tokens.ALPHABET)
        for banned in "ILOU":
            self.assertNotIn(banned, "".join(groups))


class RedemptionTests(SupportAccessTestCase):

    def test_a_second_agent_cannot_redeem_an_already_redeemed_grant(self):
        code = self.mint().data["code"]
        self.assertEqual(self.redeem(code).status_code, status.HTTP_200_OK)

        other_agent_user = User.objects.create_user(
            email="second@billmunshi.test", password=PASSWORD, is_staff=True
        )
        SupportAgent.objects.create(user=other_agent_user, display_name="Second agent")

        response = self.redeem(code, agent_user=other_agent_user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            SupportAccessSession.objects.filter(agent=other_agent_user).exists()
        )

    def test_the_same_agent_resumes_their_session(self):
        code = self.mint().data["code"]
        first = self.redeem(code)
        second = self.redeem(code)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data["session"]["id"], second.data["session"]["id"])
        self.assertEqual(SupportAccessSession.objects.count(), 1)

    def test_a_non_agent_cannot_redeem(self):
        code = self.mint().data["code"]
        # is_staff without a SupportAgent row is not an agent.
        bare_staff = User.objects.create_user(
            email="bare@billmunshi.test", password=PASSWORD, is_staff=True
        )
        self.assertEqual(
            self.redeem(code, agent_user=bare_staff).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        # And neither is the client.
        self.assertEqual(
            self.redeem(code, agent_user=self.admin).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_lockout_after_five_failed_attempts(self):
        self.mint()
        for _ in range(access_tokens.MAX_FAILED_ATTEMPTS):
            failed = self.redeem("BM-0000-0000-0000")
            self.assertEqual(failed.status_code, status.HTTP_400_BAD_REQUEST)
        locked = self.redeem("BM-0000-0000-0000")
        self.assertEqual(locked.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_token_exp_never_outlives_the_grant(self):
        token, grant, _ = self.live_session()
        from apps.support.impersonation import ImpersonationToken

        parsed = ImpersonationToken(token)
        self.assertLessEqual(parsed["exp"], int(grant.expires_at.timestamp()) + 1)
        self.assertEqual(parsed["act"], str(self.agent_user.pk))
        self.assertEqual(parsed["sub"], str(self.admin.pk))
        self.assertEqual(parsed["scope"], grant.scope)


class AuthenticationFailsClosedTests(SupportAccessTestCase):

    def test_an_expired_grant_fails_auth_even_with_status_still_active(self):
        token, grant, session = self.live_session()

        # Exactly the state a missing sweeper leaves behind: the TTL has
        # passed, the status column has not caught up.
        SupportAccessGrant.objects.filter(pk=grant.pk).update(
            expires_at=timezone.now() - timezone.timedelta(minutes=1),
            status=SupportAccessGrant.STATUS_ACTIVE,
        )
        grant.refresh_from_db()
        self.assertEqual(grant.status, SupportAccessGrant.STATUS_ACTIVE)

        self.as_impersonator(token)
        response = self.client.get("/api/v1/me/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_revoked_grant_kills_auth_immediately(self):
        token, grant, session = self.live_session()

        self.as_impersonator(token)
        self.assertEqual(self.client.get("/api/v1/me/").status_code, status.HTTP_200_OK)

        self.client.credentials()
        self.client.force_authenticate(user=self.admin)
        revoked = self.client.post(
            reverse("support:access-grant-revoke", kwargs={"grant_id": grant.pk}),
            {"reason": "Call ended"},
            format="json",
        )
        self.assertEqual(revoked.status_code, status.HTTP_200_OK)
        self.client.force_authenticate(user=None)

        session.refresh_from_db()
        self.assertIsNotNone(session.ended_at)
        self.assertEqual(session.end_reason, SupportAccessSession.END_REVOKED)

        self.as_impersonator(token)
        self.assertEqual(
            self.client.get("/api/v1/me/").status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_closing_the_ticket_revokes_the_grant(self):
        token, grant, session = self.live_session()

        self.client.force_authenticate(user=self.agent_user)
        patched = self.client.patch(
            reverse("support_access:desk-ticket-detail", kwargs={"ticket_id": self.ticket.pk}),
            {"status": SupportTicket.STATUS_CLOSED},
            format="json",
        )
        self.assertEqual(patched.status_code, status.HTTP_200_OK)
        self.client.force_authenticate(user=None)

        grant.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(grant.status, SupportAccessGrant.STATUS_CLOSED)
        self.assertIsNotNone(session.ended_at)
        self.assertEqual(session.end_reason, SupportAccessSession.END_TICKET_CLOSED)

        self.as_impersonator(token)
        self.assertEqual(
            self.client.get("/api/v1/me/").status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_resolving_the_ticket_revokes_the_grant(self):
        _, grant, session = self.live_session()
        self.ticket.status = SupportTicket.STATUS_RESOLVED
        self.ticket.save(update_fields=["status"])
        grant.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(grant.status, SupportAccessGrant.STATUS_CLOSED)
        self.assertIsNotNone(session.ended_at)

    def test_an_ended_session_cannot_be_reused(self):
        token, _, session = self.live_session()
        session.end()
        self.as_impersonator(token)
        self.assertEqual(
            self.client.get("/api/v1/me/").status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_losing_the_agent_row_ends_access(self):
        token, _, _ = self.live_session()
        self.agent.delete()
        self.as_impersonator(token)
        self.assertEqual(
            self.client.get("/api/v1/me/").status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_an_impersonation_token_is_not_an_ordinary_access_token(self):
        from rest_framework_simplejwt.exceptions import TokenError
        from rest_framework_simplejwt.tokens import AccessToken

        token, _, _ = self.live_session()
        with self.assertRaises(TokenError):
            AccessToken(token)


class ScopeAndForbiddenEndpointsTests(SupportAccessTestCase):

    def test_a_read_only_session_is_refused_post(self):
        token, _, _ = self.live_session(scope=SupportAccessGrant.SCOPE_READ_ONLY)
        self.as_impersonator(token)

        # Reading is what read_only is for.
        self.assertEqual(self.client.get("/api/v1/me/").status_code, status.HTTP_200_OK)

        response = self.client.post(
            "/api/v1/support/tickets/",
            {"subject": "from support", "message": "written by the agent"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["code"], "IMPERSONATION_FORBIDDEN")
        self.assertEqual(SupportTicket.objects.count(), 1)

    def test_a_read_only_session_may_still_end_itself(self):
        token, _, session = self.live_session(scope=SupportAccessGrant.SCOPE_READ_ONLY)
        self.as_impersonator(token)
        response = self.client.post(reverse("support_access:desk-access-end"), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        session.refresh_from_db()
        self.assertEqual(session.end_reason, SupportAccessSession.END_SIGNED_OUT)

    def test_a_full_session_may_write_an_ordinary_endpoint(self):
        token, _, _ = self.live_session(scope=SupportAccessGrant.SCOPE_FULL)
        self.as_impersonator(token)
        response = self.client.post(
            "/api/v1/support/tickets/",
            {"subject": "from support", "message": "written by the agent"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_every_forbidden_endpoint_is_refused_at_full_scope(self):
        token, grant, _ = self.live_session(scope=SupportAccessGrant.SCOPE_FULL)
        self.as_impersonator(token)

        org_id = self.org.pk
        membership = OrgMembership.objects.get(organization=self.org, user=self.member)
        attempts = [
            # minting or revoking access grants
            ("POST", f"/api/v1/support/tickets/{self.ticket.pk}/access-grants/"),
            ("POST", f"/api/v1/support/access-grants/{grant.pk}/revoke/"),
            ("GET", f"/api/v1/organizations/{org_id}/access-grants/"),
            # password, email, 2FA
            ("POST", "/api/v1/auth/password/change/"),
            ("POST", "/api/v1/auth/password/reset/"),
            ("POST", "/api/v1/auth/verify-email/resend/"),
            ("PATCH", "/api/v1/me/"),
            ("PUT", "/api/v1/me/"),
            # inviting, removing or re-roling members
            ("POST", f"/api/v1/org/{org_id}/members/add/"),
            ("POST", f"/api/v1/org/{org_id}/members/invite/"),
            ("DELETE", f"/api/v1/org/{org_id}/members/{membership.pk}/"),
            ("POST", f"/api/v1/org/{org_id}/members/{membership.pk}/role/"),
            ("PATCH", f"/api/v1/memberships/{membership.pk}/update/"),
            # API keys
            ("GET", f"/api/v1/org/{org_id}/api-keys/"),
            ("POST", f"/api/v1/org/{org_id}/api-keys/issue/"),
            # deleting / rewriting the organization
            ("DELETE", f"/api/v1/org/{org_id}/"),
            ("POST", f"/api/v1/org/{org_id}/update/"),
            # billing and subscriptions
            ("GET", f"/api/v1/organizations/{org_id}/subscription/"),
            ("POST", "/api/v1/subscriptions/"),
            ("POST", "/api/v1/plans/"),
        ]

        for method, path in attempts:
            with self.subTest(method=method, path=path):
                response = self.client.generic(method, path, content_type="application/json")
                self.assertEqual(
                    response.status_code,
                    status.HTTP_403_FORBIDDEN,
                    f"{method} {path} was not refused",
                )
                self.assertEqual(response.json()["code"], "IMPERSONATION_FORBIDDEN")

        # The grant is untouched by all of that.
        grant.refresh_from_db()
        self.assertEqual(grant.status, SupportAccessGrant.STATUS_ACTIVE)
        self.assertEqual(SupportAccessGrant.objects.count(), 1)

    def test_the_desk_itself_is_closed_to_an_impersonated_token(self):
        token, _, _ = self.live_session()
        self.as_impersonator(token)
        response = self.client.get(reverse("support_access:desk-tickets"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class AuditTrailTests(SupportAccessTestCase):

    def test_one_event_per_impersonated_request(self):
        token, _, session = self.live_session()
        self.as_impersonator(token)

        self.client.get("/api/v1/me/")
        self.client.get("/api/v1/me/")
        refused = self.client.patch("/api/v1/me/", {"bio": "support was here"}, format="json")
        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN)

        events = list(SupportAccessEvent.objects.filter(session=session).order_by("id"))
        self.assertEqual(len(events), 3)
        self.assertEqual([e.status_code for e in events], [200, 200, 403])
        # The refusal is in the trail, which is the point: an attempt on a
        # forbidden endpoint is the line worth reading.
        self.assertEqual(events[-1].method, "PATCH")
        self.assertEqual(events[-1].path, "/api/v1/me/")

    def test_no_event_for_an_ordinary_login(self):
        self.client.force_authenticate(user=self.admin)
        self.client.get("/api/v1/me/")
        self.assertEqual(SupportAccessEvent.objects.count(), 0)

    def test_the_client_can_see_support_is_inside(self):
        token, _, _ = self.live_session()
        self.client.force_authenticate(user=self.member)
        response = self.client.get(
            reverse("support_access:org-access-status", kwargs={"org_id": self.org.pk})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["active"])
        self.assertEqual(response.data["sessions"][0]["agent_name"], "Asha (support)")
        # The banner names the agent, not their staff email.
        self.assertNotIn(self.agent_user.email, str(response.data))


class AutoAssignmentTests(SupportAccessTestCase):
    """Assignment rides ``transaction.on_commit`` — a ticket written inside
    a request that then fails must not leave an assignment behind — so the
    tests have to let the commit hooks run."""

    def create_ticket(self, subject):
        with self.captureOnCommitCallbacks(execute=True):
            ticket = SupportTicket.objects.create(
                organization=self.org, user=self.admin, subject=subject, message=subject
            )
        ticket.refresh_from_db()
        return ticket

    def test_a_new_ticket_is_assigned_to_an_available_agent(self):
        ticket = self.create_ticket("New")
        self.assertEqual(ticket.assigned_to_id, self.agent_user.pk)
        self.assertIsNotNone(ticket.assigned_at)

    def test_an_agent_at_capacity_is_skipped(self):
        self.agent.max_open_tickets = 1
        self.agent.save(update_fields=["max_open_tickets"])
        # setUp's ticket already fills the quota.
        SupportTicket.objects.filter(pk=self.ticket.pk).update(assigned_to=self.agent_user)

        ticket = self.create_ticket("Overflow")
        self.assertIsNone(ticket.assigned_to_id)

    def test_an_inactive_agent_gets_no_new_tickets(self):
        self.agent.is_active = False
        self.agent.save(update_fields=["is_active"])
        ticket = self.create_ticket("Quiet")
        self.assertIsNone(ticket.assigned_to_id)

    def test_the_agent_who_knows_this_org_is_preferred(self):
        newcomer = User.objects.create_user(
            email="newcomer@billmunshi.test", password=PASSWORD, is_staff=True
        )
        SupportAgent.objects.create(user=newcomer, display_name="Newcomer")
        # setUp's ticket is this org's history; put it on the first agent.
        SupportTicket.objects.filter(pk=self.ticket.pk).update(
            assigned_to=self.agent_user, status=SupportTicket.STATUS_CLOSED
        )

        ticket = self.create_ticket("Again")
        self.assertEqual(ticket.assigned_to_id, self.agent_user.pk)


class RevokeIsOpenToAnyMemberTests(APITestCase):
    """Ending access is not an admin-only act.

    Minting hands out power; ending takes it back. The banner shows
    "End access" to every member, so the endpoint behind it has to
    answer to every member — otherwise the control fails at the one
    moment it exists for, when the admin who made the call has gone and
    somebody else is watching support move around their books.
    """

    def setUp(self):
        from datetime import timedelta
        from django.contrib.auth import get_user_model

        self.timedelta = timedelta
        U = get_user_model()
        self.admin = U.objects.create_user(email="revoke-admin@test.local", password="x")
        self.org = Organization.objects.create(
            name="Revoke Co", owner=self.admin, created_by=self.admin
        )
        self.member = U.objects.create_user(email="revoke-member@test.local", password="x")
        OrgMembership.objects.create(organization=self.org, user=self.admin,
                                     role=OrgMembership.ADMIN, is_active=True)
        OrgMembership.objects.create(organization=self.org, user=self.member,
                                     role=OrgMembership.ACCOUNTANT, is_active=True)
        self.outsider = U.objects.create_user(email="revoke-outsider@test.local", password="x")

        ticket = SupportTicket.objects.create(
            organization=self.org, user=self.admin, subject="s", message="m",
        )
        code, label, digest = access_tokens.mint_code()
        self.grant = SupportAccessGrant.objects.create(
            ticket=ticket, organization=self.org, created_by=self.admin,
            code_label=label, token_hash=digest,
            expires_at=timezone.now() + self.timedelta(minutes=30),
        )

    def _revoke_as(self, user):
        self.client.force_authenticate(user=user)
        return self.client.post(f"/api/v1/support/access-grants/{self.grant.id}/revoke/",
                           {}, format="json")

    def test_a_non_admin_member_can_end_access(self):
        resp = self._revoke_as(self.member)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.status, SupportAccessGrant.STATUS_REVOKED)

    def test_someone_outside_the_organization_still_cannot(self):
        resp = self._revoke_as(self.outsider)
        self.assertIn(resp.status_code, (403, 404))
        self.grant.refresh_from_db()
        self.assertNotEqual(self.grant.status, SupportAccessGrant.STATUS_REVOKED)
