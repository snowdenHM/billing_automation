"""No code may read a choices member that was never declared.

The second half of the client's 2026-11-15 failure. Three views did:

    bill.status = TallyReceiptBill.BillStatus.FAILED

and no ``BillStatus`` on any of the five Tally bill models has a FAILED
member — the four are DRAFT, ANALYSED, VERIFIED and SYNCED. Nothing
catches that until the line runs, and it only runs when something else
has already gone wrong, so it sat in the three failure handlers until a
real error reached one.

What it cost: on Python 3.11 the enum metaclass raises
``AttributeError('FAILED')``, so

* the real error (a Postgres advisory-lock overload mismatch, see
  ``test_dup_lock_key``) was replaced by the bare word "FAILED", which is
  what the operator was shown;
* the handler threw before ``bill.save()``, so the status stayed
  Analysed and the analysed data stayed on screen — the bill looked fine
  and reported as failed at the same time.

A handler that throws cannot report. This file checks the whole source
tree rather than those three lines, because the mistake is not specific
to them: a name is cheap to mistype and a failure path is rarely
exercised. Static, so it needs no database and costs nothing to run.
"""

import ast
import collections
import os
import re

from django.test import SimpleTestCase

APPS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

CHOICE_BASES = {"TextChoices", "IntegerChoices", "Choices", "Enum"}
SKIP_DIRS = {"migrations", "__pycache__", "node_modules", ".venv"}

# Attributes every member carries, plus the ones Django adds to the class.
NOT_MEMBERS = {"DoesNotExist", "MultipleObjectsReturned"}


def _python_files():
    for root, dirs, names in os.walk(APPS_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def _parse(path):
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    return source, ast.parse(source)


def _base_name(base):
    if isinstance(base, ast.Attribute):
        return base.attr
    return getattr(base, "id", "")


class GhostChoiceMemberTests(SimpleTestCase):
    """Read every ``Class.MEMBER`` and check the class declares it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.files = sorted(_python_files())

        # Declared members, by choices-class name. Keyed by name rather
        # than by import path because the reference site writes
        # ``Model.BillStatus.X`` and resolving the model would mean
        # importing the world.
        cls.declared = collections.defaultdict(set)
        for path in cls.files:
            try:
                _, tree = _parse(path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if not {_base_name(b) for b in node.bases} & CHOICE_BASES:
                    continue
                for stmt in node.body:
                    for target in getattr(stmt, "targets", []):
                        if isinstance(target, ast.Name):
                            cls.declared[node.name].add(target.id)

    def test_some_choice_classes_were_found(self):
        """Guard the guard: a broken walk would pass everything silently."""
        self.assertGreaterEqual(
            len(self.declared), 5,
            "the scan found almost no choices classes, so it is not "
            "actually checking anything",
        )
        self.assertIn("BillStatus", self.declared)

    def test_bill_status_has_no_failed_member(self):
        """The specific shape of the bug, pinned.

        If a FAILED member is ever added deliberately, this test fails and
        whoever adds it decides what the three handlers should do — rather
        than the member quietly appearing and the handlers resuming a
        behaviour nobody designed.
        """
        self.assertNotIn(
            "FAILED", self.declared["BillStatus"],
            "a FAILED bill status now exists; revisit the failure handlers "
            "in receipt_bills.py, expense_bills.py and payment_bills.py, "
            "which deliberately no longer set one",
        )

    def test_no_code_reads_an_undeclared_choice_member(self):
        offenders = []
        for path in self.files:
            try:
                source, tree = _parse(path)
            except SyntaxError:
                continue
            lines = source.splitlines()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                owner = node.value
                if not isinstance(owner, ast.Attribute):
                    continue
                if owner.attr not in self.declared:
                    continue
                name = node.attr
                if name in NOT_MEMBERS:
                    continue
                # Only SHOUTING names are member reads; `.label`, `.value`
                # and `.choices` are the enum's own API.
                if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                    continue
                if name not in self.declared[owner.attr]:
                    rel = os.path.relpath(path, APPS_DIR)
                    offenders.append(
                        f"{rel}:{node.lineno}: {owner.attr}.{name} is not "
                        f"declared (declared: "
                        f"{', '.join(sorted(self.declared[owner.attr]))})\n"
                        f"    {lines[node.lineno - 1].strip()}"
                    )

        self.assertEqual(
            offenders, [],
            "code reads a choices member that does not exist; it will raise "
            "AttributeError when that line runs:\n" + "\n".join(offenders),
        )
