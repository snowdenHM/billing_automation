"""The approval workflow, exercised over HTTP by role-holding users.

The service layer is covered by its own assertions below, but the thing
worth guarding is the round trip: a real request, a real membership row,
the real permission class. Most of the ways this feature could break --
a decorator in the wrong place, a role that quietly gains APPROVE_BILLS,
a sync gate that fires when the flag is off -- are invisible to a test
that calls the service directly.

The last of those is the regression that matters most, so it has its own
case: with ``require_bill_approval`` off, a sync request must get past
the gate untouched and fail (or succeed) for exactly the reasons it did
before this feature existed.
"""

import uuid

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.common.models import BillActivity, BillApproval
from apps.common.services import approval as approval_service
from apps.module.tally.models import TallyExpenseBill
from apps.organizations.models import Organization, OrgMembership

User = get_user_model()


def make_user(email):
    return User.objects.create_user(email=email, password="x-test-pass-123")


def make_org(name, owner):
    return Organization.objects.create(name=name, owner=owner, created_by=owner)


def make_bill(organization, uploader=None):
    return TallyExpenseBill.objects.create(
        organization=organization,
        uploaded_by=uploader,
        bill_munshi_name=f"BM-{uuid.uuid4().hex[:6]}",
        file=SimpleUploadedFile("bill.pdf", b"%PDF-1.4 test", content_type="application/pdf"),
        status=TallyExpenseBill.BillStatus.VERIFIED,
    )


class ApprovalTestBase(APITestCase):
    """One organisation holding one user per role, plus a second tenant."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user("owner@approval.test")
        cls.org = make_org("Approval Org", cls.owner)
        cls.org.require_bill_approval = True
        cls.org.save(update_fields=["require_bill_approval"])

        cls.users = {}
        for role in (OrgMembership.ADMIN, OrgMembership.MANAGER,
                     OrgMembership.ACCOUNTANT, OrgMembership.CONSULTANT):
            user = make_user(f"{role.lower()}@approval.test")
            OrgMembership.objects.create(organization=cls.org, user=user, role=role)
            cls.users[role] = user
        # A second Manager, so "somebody else approves" has somebody else.
        cls.manager2 = make_user("manager2@approval.test")
        OrgMembership.objects.create(
            organization=cls.org, user=cls.manager2, role=OrgMembership.MANAGER)

        # A separate tenant, for the cross-organisation checks.
        cls.other_owner = make_user("owner@other.test")
        cls.other_org = make_org("Other Org", cls.other_owner)
        OrgMembership.objects.create(
            organization=cls.other_org, user=cls.other_owner, role=OrgMembership.ADMIN)

    def setUp(self):
        self.bill = make_bill(self.org)

    # -- url helpers --------------------------------------------------
    def url_queue(self, org=None):
        return reverse("organizations:org-approval-queue",
                       args=[(org or self.org).id])

    def url_detail(self, bill_id, org=None):
        return reverse("organizations:org-approval-detail",
                       args=[(org or self.org).id, bill_id])

    def url_action(self, action, bill_id, org=None):
        return reverse(f"organizations:org-approval-{action}",
                       args=[(org or self.org).id, bill_id])

    def as_(self, role):
        self.client.force_authenticate(
            self.users[role] if role in self.users else role)

    def submit(self, role=OrgMembership.ACCOUNTANT, bill=None, note="ready"):
        self.as_(role)
        return self.client.post(self.url_action("submit", (bill or self.bill).id),
                                {"note": note}, format="json")

    def decide(self, action, role=OrgMembership.MANAGER, bill=None, note="ok"):
        self.as_(role)
        return self.client.post(self.url_action(action, (bill or self.bill).id),
                                {"note": note}, format="json")


class RoleMatrixTests(ApprovalTestBase):
    """Every role against every endpoint."""

    def test_who_may_submit(self):
        allowed = {OrgMembership.ADMIN, OrgMembership.MANAGER, OrgMembership.ACCOUNTANT}
        for role in self.users:
            bill = make_bill(self.org)
            response = self.submit(role, bill=bill)
            if role in allowed:
                self.assertEqual(response.status_code, status.HTTP_200_OK,
                                 f"{role} should be able to submit: {response.data}")
            else:
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN,
                                 f"{role} should not be able to submit")

    def test_who_may_decide(self):
        allowed = {OrgMembership.ADMIN, OrgMembership.MANAGER}
        for action in ("approve", "reject"):
            for role in self.users:
                bill = make_bill(self.org)
                self.submit(OrgMembership.ACCOUNTANT, bill=bill)
                response = self.decide(action, role, bill=bill, note="a reason")
                if role in allowed:
                    self.assertEqual(response.status_code, status.HTTP_200_OK,
                                     f"{role} should be able to {action}: {response.data}")
                else:
                    self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN,
                                     f"{role} should not be able to {action}")

    def test_every_role_may_read_the_queue_and_detail(self):
        self.submit()
        for role in self.users:
            self.as_(role)
            self.assertEqual(self.client.get(self.url_queue()).status_code,
                             status.HTTP_200_OK, f"{role} queue")
            self.assertEqual(self.client.get(self.url_detail(self.bill.id)).status_code,
                             status.HTTP_200_OK, f"{role} detail")

    def test_anonymous_is_refused_everywhere(self):
        self.client.force_authenticate(None)
        for url in (self.url_queue(),
                    self.url_detail(self.bill.id),
                    self.url_action("submit", self.bill.id),
                    self.url_action("approve", self.bill.id),
                    self.url_action("reject", self.bill.id)):
            response = (self.client.get(url) if "submit" not in url
                        and "approve" not in url and "reject" not in url
                        else self.client.post(url, {}, format="json"))
            self.assertIn(response.status_code,
                          (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN), url)

    def test_permissions_endpoint_matches_the_matrix(self):
        url = reverse("organizations:org-my-permissions", args=[self.org.id])
        expected = {
            OrgMembership.ADMIN: True,
            OrgMembership.MANAGER: True,
            OrgMembership.ACCOUNTANT: False,
            OrgMembership.CONSULTANT: False,
        }
        for role, may_approve in expected.items():
            self.as_(role)
            data = self.client.get(url).data
            self.assertEqual(data["role"], role)
            self.assertEqual("approve_bills" in data["permissions"], may_approve, role)
            self.assertTrue(data["require_bill_approval"])

    def test_detail_reports_can_approve_per_role(self):
        self.submit()
        for role, expected in ((OrgMembership.MANAGER, True),
                               (OrgMembership.ACCOUNTANT, False)):
            self.as_(role)
            data = self.client.get(self.url_detail(self.bill.id)).data
            self.assertEqual(data["can_approve"], expected, role)


class HappyPathTests(ApprovalTestBase):

    def test_submit_then_approve_then_sync_is_allowed(self):
        response = self.submit()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["state"], "pending")

        self.assertIsNotNone(approval_service.gate_sync(self.bill, self.org))

        response = self.decide("approve", note="checked")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["state"], "approved")
        self.assertEqual(response.data["decided_by_email"],
                         self.users[OrgMembership.MANAGER].email)

        self.assertIsNone(approval_service.gate_sync(self.bill, self.org))

    def test_reject_then_resubmit_then_approve(self):
        self.submit()
        response = self.decide("reject", note="GST number is wrong")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["state"], "rejected")

        blocked = approval_service.gate_sync(self.bill, self.org)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked[1], "APPROVAL_REJECTED")
        self.assertIn("GST number is wrong", blocked[0])

        response = self.submit(note="fixed the GST number")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["state"], "pending")
        # The old decision is cleared, not carried over.
        self.assertIsNone(response.data["decided_at"])
        self.assertEqual(response.data["decided_note"], "")

        response = self.decide("approve", note="looks right now")
        self.assertEqual(response.data["state"], "approved")
        self.assertIsNone(approval_service.gate_sync(self.bill, self.org))

    def test_detail_before_submission_is_empty_not_an_error(self):
        self.as_(OrgMembership.MANAGER)
        data = self.client.get(self.url_detail(self.bill.id)).data
        self.assertTrue(data["approval_required"])
        self.assertIsNone(data["approval"])


class RefusalTests(ApprovalTestBase):

    def test_submitter_cannot_approve_their_own_bill(self):
        # The Manager submits, then tries to sign off their own work.
        self.submit(OrgMembership.MANAGER)
        response = self.decide("approve", OrgMembership.MANAGER)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error_code"], "APPROVAL_SELF")
        # Another manager can.
        self.client.force_authenticate(self.manager2)
        response = self.client.post(self.url_action("approve", self.bill.id),
                                    {"note": "ok"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_self_rejection_is_refused_too(self):
        self.submit(OrgMembership.ADMIN)
        response = self.decide("reject", OrgMembership.ADMIN, note="my own mistake")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error_code"], "APPROVAL_SELF")

    def test_reject_without_a_reason_is_refused(self):
        self.submit()
        for note in ("", "   "):
            self.as_(OrgMembership.MANAGER)
            response = self.client.post(self.url_action("reject", self.bill.id),
                                        {"note": note}, format="json")
            self.assertEqual(response.status_code,
                             status.HTTP_422_UNPROCESSABLE_ENTITY, repr(note))
            self.assertEqual(response.data["error_code"], "APPROVAL_REASON_REQUIRED")
        # ...and with no note key at all.
        self.as_(OrgMembership.MANAGER)
        response = self.client.post(self.url_action("reject", self.bill.id),
                                    {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(approval_service.state_of(self.bill), "pending")

    def test_approve_without_a_note_is_fine(self):
        self.submit()
        self.as_(OrgMembership.MANAGER)
        response = self.client.post(self.url_action("approve", self.bill.id),
                                    {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_double_submit_is_refused(self):
        self.submit()
        response = self.submit()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error_code"], "APPROVAL_ALREADY_PENDING")

    def test_submitting_an_approved_bill_is_refused(self):
        self.submit()
        self.decide("approve")
        response = self.submit()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error_code"], "APPROVAL_ALREADY_APPROVED")
        self.assertEqual(approval_service.state_of(self.bill), "approved")

    def test_double_decide_is_refused(self):
        self.submit()
        self.decide("approve")
        response = self.decide("reject", note="changed my mind")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error_code"], "APPROVAL_ALREADY_DECIDED")
        self.assertEqual(approval_service.state_of(self.bill), "approved")

    def test_deciding_a_bill_that_was_never_submitted_is_refused(self):
        response = self.decide("approve")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error_code"], "APPROVAL_NOT_SUBMITTED")
        self.assertFalse(BillApproval.objects.filter(bill_id=self.bill.pk).exists())

    def test_unknown_bill_id_is_404(self):
        ghost = uuid.uuid4()
        self.as_(OrgMembership.MANAGER)
        for url in (self.url_detail(ghost),
                    self.url_action("submit", ghost),
                    self.url_action("approve", ghost)):
            response = (self.client.get(url) if url.endswith(f"{ghost}/")
                        else self.client.post(url, {"note": "x"}, format="json"))
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, url)
            self.assertEqual(response.data["error_code"], "BILL_NOT_FOUND")


class CrossTenantTests(ApprovalTestBase):
    """A bill id from another organisation must not be reachable."""

    def setUp(self):
        super().setUp()
        self.foreign_bill = make_bill(self.other_org)

    def test_foreign_bill_is_404_not_a_leak(self):
        self.as_(OrgMembership.ADMIN)
        for action in ("submit", "approve", "reject"):
            response = self.client.post(
                self.url_action(action, self.foreign_bill.id),
                {"note": "x"}, format="json")
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, action)
            self.assertEqual(response.data["error_code"], "BILL_NOT_FOUND")

        response = self.client.get(self.url_detail(self.foreign_bill.id))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(BillApproval.objects.filter(bill_id=self.foreign_bill.pk).exists())

    def test_non_member_cannot_reach_another_orgs_endpoints(self):
        self.client.force_authenticate(self.other_owner)
        response = self.client.get(self.url_queue())
        self.assertIn(response.status_code,
                      (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))

    def test_queue_only_shows_this_organisation(self):
        self.submit()
        approval_service.submit(self.foreign_bill, actor=self.other_owner,
                                organization=self.other_org)
        self.as_(OrgMembership.MANAGER)
        rows = self.client.get(self.url_queue()).data["results"]
        self.assertEqual([r["bill_id"] for r in rows], [str(self.bill.id)])


class OneRowPerBillTests(ApprovalTestBase):

    def test_the_lifecycle_never_creates_a_second_row(self):
        self.submit()
        self.decide("reject", note="wrong total")
        self.submit(note="corrected")
        self.decide("approve")
        self.assertEqual(BillApproval.objects.filter(bill_id=self.bill.pk).count(), 1)

    def test_the_database_constraint_holds(self):
        self.submit()
        row = BillApproval.objects.get(bill_id=self.bill.pk)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BillApproval.objects.create(
                    organization=self.org, module=row.module,
                    bill_type=row.bill_type, bill_id=row.bill_id,
                )


class ActivityLogTests(ApprovalTestBase):

    def actions(self):
        return list(
            BillActivity.objects
            .filter(bill_id=self.bill.pk)
            .order_by("created_at", "id")
            .values_list("action", flat=True)
        )

    def test_submit_approve_and_reject_are_all_recorded(self):
        self.submit()
        self.decide("reject", note="missing GST")
        self.submit(note="added GST")
        self.decide("approve", note="fine")
        self.assertEqual(self.actions(), [
            BillActivity.Action.APPROVAL_SUBMITTED,
            BillActivity.Action.REJECTED,
            BillActivity.Action.APPROVAL_SUBMITTED,
            BillActivity.Action.APPROVED,
        ])

    def test_the_actor_and_reason_are_kept(self):
        self.submit()
        self.decide("reject", note="total does not add up")
        event = BillActivity.objects.filter(
            bill_id=self.bill.pk, action=BillActivity.Action.REJECTED).first()
        self.assertEqual(event.actor, self.users[OrgMembership.MANAGER])
        self.assertEqual(event.summary, "total does not add up")

    def test_a_refused_transition_logs_nothing(self):
        self.submit()
        self.decide("approve")
        before = self.actions()
        self.decide("reject", note="too late")        # already decided
        self.submit()                                  # already approved
        self.assertEqual(self.actions(), before)


class QueueTests(ApprovalTestBase):

    def test_oldest_first_and_decided_bills_drop_out(self):
        bills = [make_bill(self.org) for _ in range(3)]
        for bill in bills:
            self.submit(bill=bill)

        self.as_(OrgMembership.MANAGER)
        rows = self.client.get(self.url_queue()).data["results"]
        self.assertEqual([r["bill_id"] for r in rows], [str(b.id) for b in bills])
        submitted = [r["submitted_at"] for r in rows]
        self.assertEqual(submitted, sorted(submitted))

        self.decide("approve", bill=bills[0])
        self.decide("reject", bill=bills[1], note="send it back")
        rows = self.client.get(self.url_queue()).data["results"]
        self.assertEqual([r["bill_id"] for r in rows], [str(bills[2].id)])

    def test_queue_reports_whether_approval_is_required(self):
        self.as_(OrgMembership.MANAGER)
        self.assertTrue(self.client.get(self.url_queue()).data["approval_required"])
        self.org.require_bill_approval = False
        self.org.save(update_fields=["require_bill_approval"])
        self.assertFalse(self.client.get(self.url_queue()).data["approval_required"])

    def test_module_filter(self):
        self.submit()
        self.as_(OrgMembership.MANAGER)
        self.assertEqual(
            len(self.client.get(self.url_queue(), {"module": "tally"}).data["results"]), 1)
        self.assertEqual(
            len(self.client.get(self.url_queue(), {"module": "zoho"}).data["results"]), 0)


class SyncGateTests(ApprovalTestBase):
    """The gate on the real Tally sync endpoint.

    The test stops at the gate rather than driving a sync to completion:
    past it the view needs an analysed bill and a Tally connection, and
    neither belongs in a unit test. What matters here is only whether the
    request is stopped, and by what.
    """

    def sync(self, bill=None, role=OrgMembership.MANAGER):
        self.as_(role)
        return self.client.post(
            reverse("tally:expense-bill-sync", args=[self.org.id]),
            {"bill_id": str((bill or self.bill).id)}, format="json")

    def set_flag(self, value):
        self.org.require_bill_approval = value
        self.org.save(update_fields=["require_bill_approval"])

    def test_flag_on_blocks_a_bill_that_was_never_submitted(self):
        response = self.sync()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error_code"], "APPROVAL_NOT_SUBMITTED")
        self.assertIn("submitted for approval", response.data["message"])

    def test_flag_on_blocks_a_pending_bill(self):
        self.submit()
        response = self.sync()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error_code"], "APPROVAL_PENDING")

    def test_flag_on_blocks_a_rejected_bill_and_names_the_reason(self):
        self.submit()
        self.decide("reject", note="vendor ledger is wrong")
        response = self.sync()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error_code"], "APPROVAL_REJECTED")
        self.assertIn("vendor ledger is wrong", response.data["message"])

    def test_flag_on_lets_an_approved_bill_past_the_gate(self):
        self.submit()
        self.decide("approve")
        response = self.sync()
        self.assertNotEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertNotIn("APPROVAL", str(response.data.get("error_code", "")))

    def test_flag_off_is_the_old_behaviour(self):
        """The regression that matters: no gate, whatever the state."""
        self.set_flag(False)

        # Never submitted.
        with_flag_off = self.sync()
        self.assertNotEqual(with_flag_off.status_code, status.HTTP_409_CONFLICT)

        # Rejected -- still no gate, because the organisation never asked
        # for one. The request fails only for the reasons it always did.
        self.set_flag(True)
        self.submit()
        self.decide("reject", note="no")
        self.set_flag(False)
        response = self.sync()
        self.assertEqual(response.status_code, with_flag_off.status_code)
        self.assertEqual(response.data.get("error_code"),
                         with_flag_off.data.get("error_code"))
        self.assertIsNone(approval_service.gate_sync(self.bill, self.org))

    def test_an_accountant_still_cannot_sync(self):
        self.submit()
        self.decide("approve")
        response = self.sync(role=OrgMembership.ACCOUNTANT)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ConcurrencyTests(ApprovalTestBase):
    """Two submits landing at once must not become a 500.

    ``select_for_update`` cannot lock a row that does not exist yet, so
    the first submit for a bill has no lock to take and a double-clicked
    button can get two requests past the "already pending" check. The
    unique constraint then rejects one of them, and the caller should
    see the ordinary conflict rather than a server error.

    Simulated by making the lookup miss a row that is really there,
    which is exactly what the losing request sees.
    """

    def test_a_losing_concurrent_submit_gets_a_conflict_not_a_500(self):
        from unittest import mock

        from django.db.models import Manager

        self.submit()                      # the row now exists
        with mock.patch.object(Manager, "select_for_update",
                               return_value=BillApproval.objects.none()):
            response = self.submit()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error_code"], "APPROVAL_ALREADY_PENDING")
        self.assertEqual(BillApproval.objects.filter(bill_id=self.bill.pk).count(), 1)


class MembershipTests(ApprovalTestBase):

    def test_a_deactivated_member_loses_access(self):
        self.submit()
        OrgMembership.objects.filter(
            organization=self.org, user=self.users[OrgMembership.MANAGER],
        ).update(is_active=False)
        self.as_(OrgMembership.MANAGER)
        response = self.client.post(self.url_action("approve", self.bill.id),
                                    {"note": "ok"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_role_change_takes_effect_immediately(self):
        self.submit()
        OrgMembership.objects.filter(
            organization=self.org, user=self.users[OrgMembership.ACCOUNTANT],
        ).update(role=OrgMembership.MANAGER)
        self.as_(OrgMembership.ACCOUNTANT)
        # They submitted it, so self-approval still stops them -- but on
        # the approval rule now, not on the role.
        response = self.client.post(self.url_action("approve", self.bill.id),
                                    {"note": "ok"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error_code"], "APPROVAL_SELF")


class BillKindCoverageTests(ApprovalTestBase):
    """Every bill the approval endpoints accept must resolve to a kind."""

    def test_all_eight_bill_models_resolve(self):
        from apps.common.services.activity import resolve_bill_kind
        from apps.common.views_approval import _bill_models

        for model in _bill_models():
            module, bill_type = resolve_bill_kind(model())
            self.assertIsNotNone(module, model.__name__)
            self.assertIsNotNone(bill_type, model.__name__)
