"""The duplicate-check advisory lock must be callable for every organization.

Reported by a client on 2026-11-15: a bill uploaded, the analysis
visibly succeeded and its data was on screen, and the operator was told
"AI analysis failed: FAILED".

The production log, once the UTC/IST offset in the search was corrected:

    ERROR apps.module.tally.views.receipt_bills:
      Error processing receipt analysis data:
      function pg_advisory_xact_lock(integer, bigint) does not exist
      LINE 1: SELECT pg_advisory_xact_lock(1835101793, 2567467722)
    ERROR apps.module.tally.tasks:
      AI analysis failed for receipt bill 9f064179-...: FAILED

Two separate faults, stacked:

* ``2567467722`` is the first eight hex digits of the org's UUID
  (``990872ca``). Postgres only has ``pg_advisory_xact_lock(bigint)`` and
  ``(int, int)``, and that ``int`` is signed — it stops at 2147483647.
  psycopg therefore sent a bigint for the second argument and no overload
  matched. Every organization whose UUID begins 8-f hit this, roughly
  half of them, and the aborted transaction took the analysis with it.
* The handler that caught it then did
  ``bill.status = BillStatus.FAILED``, a member that does not exist on
  any of the five bill models. The enum raised
  ``AttributeError('FAILED')``, which replaced the real error with the
  bare word "FAILED" and stopped the status being saved — which is why
  the bill still read as Analysed with its data intact.

This file covers the first fault. ``test_no_ghost_choice_members``
covers the second.
"""

from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase

from apps.common.services.duplicate_detection import (
    _DUPLICATE_LOCK_NAMESPACE, _organization_dup_lock, check_duplicate_bill,
    dup_lock_key,
)

SIGNED_BIGINT_MAX = 2 ** 63 - 1

# The organization from the production log, and the boundary cases around
# the signed 32-bit limit the old code fell off.
CLIENT_ORG = "990872ca-7d2e-4b1f-9a33-1c4e5f6a7b8c"


class DupLockKeyTests(SimpleTestCase):

    def test_the_clients_organization_produces_a_usable_key(self):
        key = dup_lock_key(CLIENT_ORG)
        self.assertLessEqual(
            key, SIGNED_BIGINT_MAX,
            "the lock key does not fit a signed bigint, so Postgres has no "
            "matching pg_advisory_xact_lock overload",
        )
        self.assertGreater(key, 0, "a negative key would be a different lock")

    def test_every_org_half_fits_a_signed_bigint(self):
        """The whole 32-bit range, not just the half that happened to work.

        Written as a sweep rather than one example because the bug was a
        range error: the first half of the space worked perfectly and that
        is exactly why it shipped.
        """
        for high in "0123456789abcdef":
            org = f"{high}f0872ca-7d2e-4b1f-9a33-1c4e5f6a7b8c"
            key = dup_lock_key(org)
            self.assertGreater(key, 0, org)
            self.assertLessEqual(
                key, SIGNED_BIGINT_MAX,
                f"an org whose UUID begins {high!r} cannot be locked",
            )

    def test_the_extremes_of_the_org_range_fit(self):
        for org in ("00000000-0000-0000-0000-000000000000",
                    "7fffffff-0000-0000-0000-000000000000",
                    "80000000-0000-0000-0000-000000000000",
                    "ffffffff-ffff-ffff-ffff-ffffffffffff"):
            key = dup_lock_key(org)
            self.assertTrue(0 < key <= SIGNED_BIGINT_MAX, f"{org} -> {key}")

    def test_different_organizations_get_different_locks(self):
        """Two orgs sharing a lock would serialize uploads across tenants."""
        keys = {
            dup_lock_key(f"{h}f0872ca-7d2e-4b1f-9a33-1c4e5f6a7b8c")
            for h in "0123456789abcdef"
        }
        self.assertEqual(len(keys), 16, "two organizations collided on one key")

    def test_the_namespace_is_preserved(self):
        """Packing the two halves must not lose the namespace."""
        key = dup_lock_key(CLIENT_ORG)
        self.assertEqual(key >> 32, _DUPLICATE_LOCK_NAMESPACE)
        self.assertEqual(key & 0xFFFFFFFF, 0x990872ca)

    def test_a_non_uuid_organization_id_still_yields_a_key(self):
        """The fallback path must not reintroduce the overflow.

        ``abs(hash(...))`` is unbounded, so it is masked to 32 bits; if it
        were not, the shift would push the key past the signed limit and
        the original error would be back by another route.
        """
        for odd in ("not-a-uuid", "", 12345, None):
            key = dup_lock_key(odd)
            self.assertTrue(
                0 < key <= SIGNED_BIGINT_MAX, f"{odd!r} -> {key}",
            )

    def test_the_key_is_stable(self):
        """A lock is only a lock if the same org computes the same key."""
        self.assertEqual(dup_lock_key(CLIENT_ORG), dup_lock_key(CLIENT_ORG))


class DupLockAcquisitionTests(TransactionTestCase):
    """Actually take the lock, against the real Postgres.

    The arithmetic tests above cannot see the fault that shipped: the old
    code's key *value* was fine, it was the two-argument call signature
    that had no matching overload. Only the database can say whether
    ``SELECT pg_advisory_xact_lock(...)`` resolves, so this is the test
    that would have caught the client's bug, and the one to keep.
    """

    def test_the_lock_is_acquired_for_an_organization_in_the_upper_half(self):
        """``990872ca`` — the client's own org, which used to fail here."""
        with _organization_dup_lock(CLIENT_ORG):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND objid IS NOT NULL",
                )
                self.assertGreaterEqual(
                    cursor.fetchone()[0], 1, "no advisory lock was taken",
                )

    def test_the_lock_is_acquired_across_the_whole_range(self):
        for high in "0123456789abcdef":
            org = f"{high}f0872ca-7d2e-4b1f-9a33-1c4e5f6a7b8c"
            with self.subTest(org=org):
                with _organization_dup_lock(org):
                    pass          # resolving the call is the assertion

    def test_the_lock_is_released_with_the_transaction(self):
        """Transaction-scoped: a leaked lock would stall the next upload."""
        with _organization_dup_lock(CLIENT_ORG):
            pass
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND objid IS NOT NULL AND pid = pg_backend_pid()",
            )
            self.assertEqual(
                cursor.fetchone()[0], 0, "an advisory lock outlived its transaction",
            )

    def test_a_duplicate_check_runs_end_to_end_for_such_an_organization(self):
        """The path the client actually hit, not just the lock.

        ``check_duplicate_bill`` returns early unless the bill carries an
        invoice number or a vendor name, so the data below is the minimum
        that reaches the locked section.
        """
        from apps.module.tally.models import TallyVendorBill
        from apps.organizations.models import Organization
        from django.contrib.auth import get_user_model

        owner = get_user_model().objects.create_user(
            email="duplock@tally.test", password="x-test-pass-123",
        )
        org = Organization.objects.create(
            name="Lock Org", owner=owner, created_by=owner,
        )
        bill = TallyVendorBill.objects.create(
            organization=org,
            analysed_data={"invoice_number": "INV-1", "vendor_name": "ACME"},
        )
        # Not asserting the verdict — only that it returns one instead of
        # raising ProgrammingError and aborting the analysis.
        is_duplicate, matches, score = check_duplicate_bill(
            bill, org, TallyVendorBill,
        )
        self.assertIsInstance(is_duplicate, bool)
