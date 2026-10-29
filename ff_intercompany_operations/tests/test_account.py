from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged("ff_intercompany_operations", "at_install", "-post_install")
class TestIntercompanyAccount(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The shared test database contains legacy NOT NULL partner columns
        # from an addon that is not loaded in this registry. Keep this
        # compatibility setup scoped to the test database; production code
        # does not depend on those columns.
        for column in ("group_rfq", "group_on"):
            cls.env.cr.execute(
                "ALTER TABLE res_partner ALTER COLUMN %s SET DEFAULT 'default'" % column
            )
        # Same legacy-schema compatibility for company creation in this
        # isolated database (these fields belong to an addon not loaded here).
        cls.env.cr.execute(
            "ALTER TABLE res_company ALTER COLUMN sale_invoice_policy SET DEFAULT 'order'"
        )
        cls.env.cr.execute(
            "ALTER TABLE res_company ALTER COLUMN security_lead SET DEFAULT 0"
        )
        cls.company_a = cls.env.company
        cls.company_b = cls.env["res.company"].search([("id", "!=", cls.company_a.id)], limit=1)
        if not cls.company_b:
            partner = cls.env["res.partner"].create({"name": "IC Accounting Company B"})
            cls.company_b = cls.env["res.company"].create({"name": "IC Accounting Company B", "partner_id": partner.id, "currency_id": cls.company_a.currency_id.id})
        # Keep the test isolated from agreements created manually in the
        # shared development database.  The production constraint correctly
        # permits only one active agreement per company pair.
        pair_key = ":".join(str(company_id) for company_id in sorted((cls.company_a.id, cls.company_b.id)))
        cls.env["ff.intercompany.agreement"].search([
            ("company_pair_key", "=", pair_key),
            ("active", "=", True),
        ]).write({"active": False, "status": "expired"})
        cls.env["ff.intercompany.route"].search([
            "|",
            "&", ("source_company_id", "=", cls.company_a.id), ("destination_company_id", "=", cls.company_b.id),
            "&", ("source_company_id", "=", cls.company_b.id), ("destination_company_id", "=", cls.company_a.id),
        ]).write({"active": False})
        if not cls.env["account.journal"].search([("company_id", "=", cls.company_b.id), ("type", "=", "purchase")], limit=1):
            cls.env["account.journal"].create({
                "name": "IC Purchases B",
                "code": "ICPB",
                "type": "purchase",
                "company_id": cls.company_b.id,
            })
        if not cls.env["account.journal"].search([("company_id", "=", cls.company_a.id), ("type", "=", "sale")], limit=1):
            cls.env["account.journal"].create({
                "name": "IC Sales A",
                "code": "ICSA",
                "type": "sale",
                "company_id": cls.company_a.id,
            })
        cls.customer = cls.env["res.partner"].create({"name": "Accounting Customer A", "company_id": cls.company_a.id, "is_company": True})
        cls.vendor = cls.env["res.partner"].create({"name": "Accounting Vendor B", "company_id": cls.company_b.id, "is_company": True})
        cls.agreement = cls.env["ff.intercompany.agreement"].create({"name": "Accounting Agreement", "company_a_id": cls.company_a.id, "company_b_id": cls.company_b.id, "status": "active"})
        cls.mapping = cls.env["ff.intercompany.counterparty"].create({"name": "Accounting Mapping", "agreement_id": cls.agreement.id, "company_a_id": cls.company_a.id, "company_b_id": cls.company_b.id, "partner_a_id": cls.customer.id, "partner_b_id": cls.vendor.id})
        cls.route = cls.env["ff.intercompany.route"].create({"name": "Invoice to Bill", "agreement_id": cls.agreement.id, "counterparty_mapping_id": cls.mapping.id, "source_company_id": cls.company_a.id, "destination_company_id": cls.company_b.id, "source_document": "account.move", "target_document": "account.move", "trigger": "posting", "source_partner_id": cls.customer.id, "destination_partner_id": cls.vendor.id})

    def test_posted_invoice_creates_one_draft_bill(self):
        journal = self.env["account.journal"].search([("company_id", "=", self.company_a.id), ("type", "=", "sale")], limit=1)
        self.assertTrue(journal)
        income_account = self.env["account.account"].search([("company_ids", "in", self.company_a.id), ("account_type", "=", "income")], limit=1)
        if not income_account:
            income_account = self.env["account.account"].create({
                "name": "IC Service Income A",
                "code": "ICINC",
                "account_type": "income",
                "company_ids": [(4, self.company_a.id)],
            })
        self.assertTrue(income_account)
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "company_id": self.company_a.id, "journal_id": journal.id,
            "partner_id": self.customer.id, "invoice_line_ids": [(0, 0, {
                "name": "Intercompany service", "quantity": 2, "price_unit": 100, "account_id": income_account.id,
            })],
        })
        invoice.action_post()
        event = self.env["ff.intercompany.event"].search([("source_model", "=", "account.move"), ("source_res_id", "=", invoice.id)], limit=1)
        self.assertEqual(event.state, "completed")
        bill = self.env["account.move"].browse(event.target_res_id)
        self.assertEqual(bill.move_type, "in_invoice")
        self.assertEqual(bill.state, "draft")
        self.assertEqual(self.env["account.move"].search_count([("ref", "=", invoice.name), ("move_type", "=", "in_invoice")]), 1)

    def test_manual_invoice_route_requires_explicit_sync(self):
        self.route.write({"trigger": "manual"})
        journal = self.env["account.journal"].search([("company_id", "=", self.company_a.id), ("type", "=", "sale")], limit=1)
        income_account = self.env["account.account"].search([("company_ids", "in", self.company_a.id), ("account_type", "=", "income")], limit=1)
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "company_id": self.company_a.id, "journal_id": journal.id,
            "partner_id": self.customer.id, "invoice_line_ids": [(0, 0, {
                "name": "Manual intercompany service", "quantity": 1, "price_unit": 50, "account_id": income_account.id,
            })],
        })
        invoice.action_post()
        self.assertFalse(self.env["ff.intercompany.event"].search([
            ("source_model", "=", "account.move"), ("source_res_id", "=", invoice.id),
        ]))
        invoice.with_user(self.env.ref("base.user_admin")).action_intercompany_sync()
        event = self.env["ff.intercompany.event"].search([
            ("source_model", "=", "account.move"), ("source_res_id", "=", invoice.id),
        ], limit=1)
        self.assertEqual(event.state, "completed")
        self.assertEqual(self.env["account.move"].browse(event.target_res_id).state, "draft")

    def test_reverse_and_modify_syncs_credit_posted_via_internal_post(self):
        journal = self.env["account.journal"].search([
            ("company_id", "=", self.company_a.id), ("type", "=", "sale")
        ], limit=1)
        income_account = self.env["account.account"].search([
            ("company_ids", "in", self.company_a.id), ("account_type", "=", "income")
        ], limit=1)
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice",
            "company_id": self.company_a.id,
            "journal_id": journal.id,
            "partner_id": self.customer.id,
            "invoice_line_ids": [(0, 0, {
                "name": "Invoice to reverse and modify",
                "quantity": 10,
                "price_unit": 250,
                "account_id": income_account.id,
            })],
        })
        invoice.action_post()

        wizard = self.env["account.move.reversal"].create({
            "move_ids": [(6, 0, invoice.ids)],
            "company_id": self.company_a.id,
            "journal_id": invoice.journal_id.id,
            "date": fields.Date.context_today(self.env.user),
            "reason": "Intercompany Reverse and Modify regression test",
        })
        wizard.reverse_moves(is_modify=True)

        credit = invoice.reversal_move_ids.filtered(
            lambda move: move.move_type == "out_refund"
        )
        self.assertEqual(len(credit), 1)
        self.assertEqual(credit.state, "posted")
        event = self.env["ff.intercompany.event"].search([
            ("source_model", "=", "account.move"),
            ("source_res_id", "=", credit.id),
        ], limit=1)
        self.assertTrue(event, "The credit created by Reverse and Modify must be synchronized")
        self.assertEqual(event.state, "completed")
        counterpart = self.env["account.move"].browse(event.target_res_id)
        self.assertEqual(counterpart.move_type, "in_refund")
        self.assertEqual(counterpart.company_id, self.company_b)
        self.assertEqual(counterpart.state, "draft")
        self.assertEqual(counterpart.invoice_line_ids.filtered(
            lambda line: line.display_type == "product"
        ).quantity, 10)
