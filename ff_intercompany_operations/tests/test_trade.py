from odoo.tests.common import TransactionCase, tagged
from datetime import date, timedelta
from odoo import fields

from odoo.exceptions import AccessError, UserError, ValidationError


@tagged("ff_intercompany_operations", "at_install", "-post_install")
class TestIntercompanyTrade(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.company
        # Keep pre-existing demo/local routes from hijacking this isolated
        # source company during route resolution.
        cls.env["ff.intercompany.route"].search([
            ("source_company_id", "=", cls.company_a.id),
        ]).write({"active": False})
        cls.company_b = cls.env["res.company"].create({"name": "IC Test Company B", "partner_id": cls.env["res.partner"].create({"name": "IC Test Company B"}).id, "currency_id": cls.company_a.currency_id.id, "country_id": cls.company_a.country_id.id})
        destination_tax_group = cls.env["account.tax.group"].search([
            ("company_id", "=", cls.company_b.id)
        ], limit=1)
        if not destination_tax_group:
            destination_tax_group = cls.env["account.tax.group"].with_company(cls.company_b).create({
                "name": "IC Test Company B Taxes",
                "company_id": cls.company_b.id,
            })
        tax_service = cls.env["ff.intercompany.account.service"]
        for source_use, target_use in (("sale", "purchase"), ("purchase", "sale")):
            source_taxes = cls.env["account.tax"].search([
                ("company_id", "=", cls.company_a.id),
                ("type_tax_use", "=", source_use),
                ("active", "=", True),
                ("amount_type", "!=", "group"),
            ])
            for source_tax in source_taxes:
                signature = tax_service._tax_calculation_signature(source_tax)
                destination_taxes = cls.env["account.tax"].search([
                    ("company_id", "=", cls.company_b.id),
                    ("type_tax_use", "=", target_use),
                    ("active", "=", True),
                ])
                if destination_taxes.filtered(
                    lambda tax: tax_service._tax_calculation_signature(tax) == signature
                ):
                    continue
                tax_values = {
                    "name": source_tax.name,
                    "amount": source_tax.amount,
                    "amount_type": source_tax.amount_type,
                    "type_tax_use": target_use,
                    "company_id": cls.company_b.id,
                    "tax_group_id": destination_tax_group.id,
                    "country_id": cls.company_b.country_id.id,
                    "sequence": source_tax.sequence,
                    "price_include_override": source_tax.price_include_override,
                    "include_base_amount": source_tax.include_base_amount,
                    "is_base_affected": source_tax.is_base_affected,
                    "tax_exigibility": source_tax.tax_exigibility,
                    "tax_scope": source_tax.tax_scope,
                }
                for optional_field in ("is_domestic", "is_withholding_tax"):
                    if optional_field in source_tax._fields:
                        tax_values[optional_field] = source_tax[optional_field]
                cls.env["account.tax"].with_company(cls.company_b).create(tax_values)
        cls.destination_warehouse = cls.env["stock.warehouse"].search([
            ("company_id", "=", cls.company_b.id),
        ], limit=1)
        if not cls.destination_warehouse:
            cls.destination_warehouse = cls.env["stock.warehouse"].with_company(cls.company_b).create({
                "name": "IC Test Warehouse B",
                "code": "ICTB",
            })
        cls.customer_b = cls.env["res.partner"].create({"name": "Company B Customer", "company_id": cls.company_a.id, "is_company": True})
        cls.vendor_a = cls.env["res.partner"].create({"name": "Company A Vendor", "company_id": cls.company_b.id, "is_company": True})
        cls.product = cls.env["product.product"].create({"name": "Shared IC Product", "type": "consu", "list_price": 150, "standard_price": 100})
        cls.agreement = cls.env["ff.intercompany.agreement"].create({"name": "Test Agreement", "company_a_id": cls.company_a.id, "company_b_id": cls.company_b.id, "status": "active"})
        cls.mapping = cls.env["ff.intercompany.counterparty"].create({"name": "A/B Counterparty", "agreement_id": cls.agreement.id, "company_a_id": cls.company_a.id, "company_b_id": cls.company_b.id, "partner_a_id": cls.customer_b.id, "partner_b_id": cls.vendor_a.id})
        cls.route = cls.env["ff.intercompany.route"].create({"name": "A SO to B PO", "agreement_id": cls.agreement.id, "counterparty_mapping_id": cls.mapping.id, "source_company_id": cls.company_a.id, "destination_company_id": cls.company_b.id, "source_document": "sale.order", "target_document": "purchase.order", "source_partner_id": cls.customer_b.id, "destination_partner_id": cls.vendor_a.id})
        cls.reverse_route = cls.env["ff.intercompany.route"].create({"name": "B PO to A SO", "agreement_id": cls.agreement.id, "counterparty_mapping_id": cls.mapping.id, "source_company_id": cls.company_b.id, "destination_company_id": cls.company_a.id, "source_document": "purchase.order", "target_document": "sale.order", "source_partner_id": cls.vendor_a.id, "destination_partner_id": cls.customer_b.id})

    def test_agreement_rejects_same_company(self):
        with self.assertRaises(ValidationError):
            self.env["ff.intercompany.agreement"].create({"name": "Invalid", "company_a_id": self.company_a.id, "company_b_id": self.company_a.id})

    def test_sales_confirmation_creates_one_purchase_order(self):
        order = self.env["sale.order"].create({"company_id": self.company_a.id, "partner_id": self.customer_b.id, "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 2, "price_unit": 100})]})
        order.action_confirm()
        event = self.env["ff.intercompany.event"].search([("source_model", "=", "sale.order"), ("source_res_id", "=", order.id)], limit=1)
        self.assertEqual(event.state, "completed")
        target = self.env["purchase.order"].search([("origin", "ilike", order.name)], limit=1)
        self.assertTrue(target)
        self.assertEqual(target.picking_type_id, self.destination_warehouse.in_type_id)
        self.assertEqual(self.env["purchase.order"].search_count([("origin", "ilike", order.name)]), 1)
        duplicate = self.env["ff.intercompany.event"].create_for_document(self.route, order, "confirmation")
        self.assertEqual(duplicate.id, event.id)
        self.assertEqual(self.env["purchase.order"].search_count([("origin", "ilike", order.name)]), 1)
        self.assertEqual(self.env["ff.intercompany.event"].search_count([
            ("source_model", "=", "purchase.order"),
            ("event_type", "=", "confirmation"),
        ]), 0)

    def test_trade_routes_map_taxes_between_company_sale_and_purchase_taxes(self):
        def tax_group_for(company):
            group = self.env["account.tax.group"].search([
                ("company_id", "=", company.id)
            ], limit=1)
            return group or self.env["account.tax.group"].with_company(company).create({
                "name": "IC Test Tax Group %s" % company.id,
                "company_id": company.id,
            })

        source_sale_tax = self.env["account.tax"].with_company(self.company_a).create({
            "name": "IC Source VAT 13.77%",
            "amount": 13.77,
            "amount_type": "percent",
            "type_tax_use": "sale",
            "company_id": self.company_a.id,
            "tax_group_id": tax_group_for(self.company_a).id,
        })
        destination_purchase_tax = self.env["account.tax"].with_company(self.company_b).create({
            "name": "IC Destination VAT 13.77%",
            "amount": 13.77,
            "amount_type": "percent",
            "type_tax_use": "purchase",
            "company_id": self.company_b.id,
            "tax_group_id": tax_group_for(self.company_b).id,
        })
        order = self.env["sale.order"].create({
            "company_id": self.company_a.id,
            "partner_id": self.customer_b.id,
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "product_uom_qty": 1,
                "price_unit": 100,
                "tax_ids": [(6, 0, source_sale_tax.ids)],
            })],
        })
        order.action_confirm()
        purchase = self.env["purchase.order"].search([
            ("origin", "ilike", order.name), ("company_id", "=", self.company_b.id)
        ], limit=1)
        self.assertEqual(purchase.order_line.filtered(
            lambda line: not line.display_type
        ).tax_ids, destination_purchase_tax)
        self.assertAlmostEqual(order.amount_total, purchase.amount_total, places=2)

        reverse_purchase = self.env["purchase.order"].with_company(self.company_b).create({
            "company_id": self.company_b.id,
            "partner_id": self.vendor_a.id,
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "product_qty": 1,
                "price_unit": 100,
                "tax_ids": [(6, 0, destination_purchase_tax.ids)],
            })],
        })
        reverse_purchase.button_confirm()
        reverse_sale = self.env["sale.order"].search([
            ("client_order_ref", "ilike", reverse_purchase.name),
            ("company_id", "=", self.company_a.id),
        ], limit=1)
        self.assertEqual(reverse_sale.order_line.filtered(
            lambda line: not line.display_type
        ).tax_ids, source_sale_tax)

    def test_purchase_confirmation_creates_one_sales_order(self):
        purchase = self.env["purchase.order"].with_company(self.company_b).create({
            "company_id": self.company_b.id,
            "partner_id": self.vendor_a.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_qty": 3, "price_unit": 100})],
        })
        purchase.button_confirm()
        event = self.env["ff.intercompany.event"].search([("source_model", "=", "purchase.order"), ("source_res_id", "=", purchase.id)], limit=1)
        self.assertEqual(event.state, "completed")
        self.assertEqual(self.env["sale.order"].search_count([("client_order_ref", "ilike", purchase.name)]), 1)

    def test_regular_user_cannot_read_route_configuration(self):
        user = self.env["res.users"].create({
            "name": "Intercompany Limited User",
            "login": "ic_limited_user",
            "group_ids": [self.env.ref("base.group_user").id, self.env.ref("ff_intercompany_operations.group_intercompany_user").id],
            "company_id": self.company_a.id,
            "company_ids": [(6, 0, [self.company_a.id])],
        })
        with self.assertRaises(AccessError):
            self.env["ff.intercompany.route"].with_user(user).search([])

    def test_counterparty_marker_is_visible_on_mapped_contacts(self):
        self.assertTrue(self.customer_b.is_intercompany_counterparty)
        self.assertTrue(self.vendor_a.is_intercompany_counterparty)

    def test_auto_policy_confirms_commercial_counterpart_only(self):
        self.route.write({"active": False})
        auto_route = self.env["ff.intercompany.route"].create({
            "name": "A SO to B PO Auto", "sequence": 1, "agreement_id": self.agreement.id,
            "counterparty_mapping_id": self.mapping.id, "source_company_id": self.company_a.id,
            "destination_company_id": self.company_b.id, "source_document": "sale.order",
            "target_document": "purchase.order", "source_partner_id": self.customer_b.id,
            "destination_partner_id": self.vendor_a.id, "approval_policy": "auto",
        })
        order = self.env["sale.order"].create({"company_id": self.company_a.id, "partner_id": self.customer_b.id, "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1, "price_unit": 100})]})
        order.action_confirm()
        target = self.env["purchase.order"].search([("origin", "ilike", order.name)], limit=1)
        self.assertEqual(target.state, "purchase")
        self.assertTrue(target.picking_ids and all(p.state != "done" for p in target.picking_ids))

    def test_expired_agreement_does_not_sync(self):
        self.agreement.write({
            "effective_date": date.today() - timedelta(days=30),
            "expiration_date": date.today() - timedelta(days=1),
        })
        order = self.env["sale.order"].create({
            "company_id": self.company_a.id,
            "partner_id": self.customer_b.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1, "price_unit": 100})],
        })
        order.action_confirm()
        self.assertFalse(self.env["ff.intercompany.event"].search([
            ("source_model", "=", "sale.order"), ("source_res_id", "=", order.id),
        ]))

    def test_manual_route_is_created_only_when_requested(self):
        self.route.write({"trigger": "manual"})
        order = self.env["sale.order"].create({
            "company_id": self.company_a.id,
            "partner_id": self.customer_b.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1, "price_unit": 100})],
        })
        order.action_confirm()
        self.assertFalse(self.env["ff.intercompany.event"].search([
            ("source_model", "=", "sale.order"), ("source_res_id", "=", order.id),
        ]))
        order.with_user(self.env.ref("base.user_admin")).action_intercompany_sync()
        event = self.env["ff.intercompany.event"].search([
            ("source_model", "=", "sale.order"), ("source_res_id", "=", order.id),
        ], limit=1)
        self.assertEqual(event.state, "completed")

    def test_agreed_pricing_uses_destination_product_price(self):
        self.route.write({"pricing_policy": "agreed"})
        self.product.with_company(self.company_b).write({"list_price": 175})
        order = self.env["sale.order"].create({
            "company_id": self.company_a.id,
            "partner_id": self.customer_b.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1, "price_unit": 100})],
        })
        order.action_confirm()
        target = self.env["purchase.order"].search([("origin", "ilike", order.name)], limit=1)
        self.assertEqual(target.order_line.price_unit, 175)

    def test_posted_customer_credit_creates_matching_vendor_credit_and_blocks_draft_reset(self):
        self.route.write({"approval_policy": "auto", "pricing_policy": "document"})

        def tax_group_for(company):
            group = self.env["account.tax.group"].search([
                ("company_id", "=", company.id)
            ], limit=1)
            return group or self.env["account.tax.group"].with_company(company).create({
                "name": "IC Credit Test Tax Group %s" % company.id,
                "company_id": company.id,
            })

        source_tax = self.env["account.tax"].with_company(self.company_a).create({
            "name": "IC Credit Output VAT 13.77%",
            "amount": 13.77,
            "amount_type": "percent",
            "type_tax_use": "sale",
            "company_id": self.company_a.id,
            "tax_group_id": tax_group_for(self.company_a).id,
        })
        destination_tax = self.env["account.tax"].with_company(self.company_b).create({
            "name": "IC Credit Input VAT 13.77%",
            "amount": 13.77,
            "amount_type": "percent",
            "type_tax_use": "purchase",
            "company_id": self.company_b.id,
            "tax_group_id": tax_group_for(self.company_b).id,
        })
        order = self.env["sale.order"].create({
            "company_id": self.company_a.id,
            "partner_id": self.customer_b.id,
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "product_uom_qty": 4,
                "price_unit": 25,
                "discount": 10,
                "tax_ids": [(6, 0, source_tax.ids)],
            })],
        })
        order.action_confirm()
        purchase_order = self.env["purchase.order"].search([
            ("origin", "ilike", order.name),
            ("company_id", "=", self.company_b.id),
        ], limit=1)
        self.assertEqual(purchase_order.state, "purchase")
        purchase_line = purchase_order.order_line.filtered(lambda line: not line.display_type)
        self.assertEqual(purchase_line.ff_intercompany_source_line_id, order.order_line)
        self.assertEqual(purchase_line.discount, 10)
        self.assertEqual(purchase_line.tax_ids, destination_tax)
        self.assertAlmostEqual(order.amount_total, purchase_order.amount_total, places=2)

        sale_journal = self.env["account.journal"].search([
            ("company_id", "=", self.company_a.id), ("type", "=", "sale"),
        ], limit=1)
        if not sale_journal:
            sale_journal = self.env["account.journal"].create({
                "name": "IC Trade Sales",
                "code": "ICTS",
                "type": "sale",
                "company_id": self.company_a.id,
            })
        purchase_journal = self.env["account.journal"].search([
            ("company_id", "=", self.company_b.id), ("type", "=", "purchase"),
        ], limit=1)
        if not purchase_journal:
            purchase_journal = self.env["account.journal"].create({
                "name": "IC Trade Purchases",
                "code": "ICTP",
                "type": "purchase",
                "company_id": self.company_b.id,
            })
        income_account = self.env["account.account"].search([
            ("company_ids", "in", self.company_a.id), ("account_type", "=", "income"),
        ], limit=1)
        if not income_account:
            income_account = self.env["account.account"].create({
                "name": "IC Trade Income",
                "code": "ICTI",
                "account_type": "income",
                "company_ids": [(4, self.company_a.id)],
            })
        expense_account = self.env["account.account"].search([
            ("company_ids", "in", self.company_b.id), ("account_type", "=", "expense"),
        ], limit=1)
        if not expense_account:
            expense_account = self.env["account.account"].create({
                "name": "IC Trade Expense",
                "code": "ICTE",
                "account_type": "expense",
                "company_ids": [(4, self.company_b.id)],
            })
        payable_account = self.env["account.account"].search([
            ("company_ids", "in", self.company_b.id),
            ("account_type", "=", "liability_payable"),
        ], limit=1)
        if not payable_account:
            payable_account = self.env["account.account"].create({
                "name": "IC Trade Payable",
                "code": "ICTP2",
                "account_type": "liability_payable",
                "reconcile": True,
                "company_ids": [(4, self.company_b.id)],
            })
        self.vendor_a.with_company(self.company_b).property_account_payable_id = payable_account

        invoice = self.env["account.move"].create({
            "move_type": "out_invoice",
            "company_id": self.company_a.id,
            "journal_id": sale_journal.id,
            "partner_id": self.customer_b.id,
            "invoice_line_ids": [(0, 0, {
                "name": "Intercompany goods",
                "product_id": self.product.id,
                "quantity": 4,
                "product_uom_id": self.product.uom_id.id,
                "price_unit": 25,
                "discount": 10,
                "account_id": income_account.id,
                "tax_ids": [(6, 0, source_tax.ids)],
                "sale_line_ids": [(6, 0, order.order_line.ids)],
            })],
        })
        invoice.action_post()
        bill_line_values = purchase_line._prepare_account_move_line()
        bill_line_values.update({
            "quantity": 4,
            "price_unit": 25,
            "discount": 10,
            "account_id": expense_account.id,
            "purchase_line_id": purchase_line.id,
        })
        vendor_bill = self.env["account.move"].with_company(self.company_b).create({
            "move_type": "in_invoice",
            "company_id": self.company_b.id,
            "journal_id": purchase_journal.id,
            "partner_id": self.vendor_a.id,
            "invoice_date": fields.Date.context_today(self.env.user),
            "invoice_line_ids": [(0, 0, bill_line_values)],
        })
        with self.assertRaises(UserError):
            invoice.button_draft()
        vendor_bill.action_post()
        self.assertEqual(vendor_bill.invoice_line_ids.filtered(
            lambda line: line.display_type == "product"
        ).tax_ids, destination_tax)
        self.assertAlmostEqual(invoice.amount_total, vendor_bill.amount_total, places=2)

        customer_credit = invoice._reverse_moves()
        customer_credit.invoice_line_ids.filtered(
            lambda line: line.product_id
        ).write({"quantity": 2})
        customer_credit.action_post()
        event = self.env["ff.intercompany.event"].search([
            ("source_model", "=", "account.move"),
            ("source_res_id", "=", customer_credit.id),
            ("route_id", "=", self.route.id),
        ], limit=1)
        self.assertEqual(event.state, "completed")
        vendor_credit = self.env["account.move"].browse(event.target_res_id)
        self.assertEqual(vendor_credit.move_type, "in_refund")
        self.assertEqual(vendor_credit.state, "posted")
        self.assertEqual(vendor_credit.invoice_line_ids.quantity, 2)
        self.assertEqual(vendor_credit.invoice_line_ids.price_unit, 25)
        self.assertEqual(vendor_credit.invoice_line_ids.discount, 10)
        self.assertEqual(vendor_credit.invoice_line_ids.filtered(
            lambda line: line.display_type == "product"
        ).tax_ids, destination_tax)
        self.assertAlmostEqual(customer_credit.amount_total, vendor_credit.amount_total, places=2)

        replacement_invoice = self.env["account.move"].create({
            "move_type": "out_invoice",
            "company_id": self.company_a.id,
            "journal_id": sale_journal.id,
            "partner_id": self.customer_b.id,
            "sab_replacement_credit_note_id": customer_credit.id,
            "invoice_line_ids": [(0, 0, {
                "name": "Replacement intercompany goods",
                "product_id": self.product.id,
                "quantity": 2,
                "product_uom_id": self.product.uom_id.id,
                "price_unit": 25,
                "discount": 10,
                "account_id": income_account.id,
                "tax_ids": [(6, 0, source_tax.ids)],
                "sale_line_ids": [(6, 0, order.order_line.ids)],
            })],
        })
        replacement_invoice.action_post()
        replacement_event = self.env["ff.intercompany.event"].search([
            ("source_model", "=", "account.move"),
            ("source_res_id", "=", replacement_invoice.id),
            ("route_id", "=", self.route.id),
        ], limit=1)
        self.assertEqual(replacement_event.state, "completed")
        replacement_bill = self.env["account.move"].browse(replacement_event.target_res_id)
        replacement_bill_line = replacement_bill.invoice_line_ids.filtered(
            lambda line: line.display_type == "product"
        )
        self.assertEqual(replacement_bill_line.quantity, 2)
        self.assertEqual(replacement_bill_line.tax_ids, destination_tax)
        self.assertAlmostEqual(replacement_invoice.amount_total, replacement_bill.amount_total, places=2)
        with self.assertRaises(UserError):
            customer_credit.button_draft()

    def test_authorized_users_restrict_synchronization(self):
        user = self.env["res.users"].create({
            "name": "Other Intercompany Manager",
            "login": "ic_other_manager",
            "group_ids": [self.env.ref("base.group_user").id, self.env.ref("ff_intercompany_operations.group_intercompany_manager").id],
            "company_id": self.company_a.id,
            "company_ids": [(6, 0, [self.company_a.id, self.company_b.id])],
        })
        self.agreement.write({"authorized_user_ids": [(6, 0, [user.id])]})
        order = self.env["sale.order"].create({
            "company_id": self.company_a.id,
            "partner_id": self.customer_b.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1, "price_unit": 100})],
        })
        with self.assertRaises(ValidationError):
            order.action_confirm()

    def test_ambiguous_routes_are_blocked(self):
        self.env["ff.intercompany.route"].create({
            "name": "Duplicate A SO to B PO", "sequence": 20, "agreement_id": self.agreement.id,
            "counterparty_mapping_id": self.mapping.id, "source_company_id": self.company_a.id,
            "destination_company_id": self.company_b.id, "source_document": "sale.order",
            "target_document": "purchase.order", "source_partner_id": self.customer_b.id,
            "destination_partner_id": self.vendor_a.id,
        })
        order = self.env["sale.order"].create({
            "company_id": self.company_a.id,
            "partner_id": self.customer_b.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1, "price_unit": 100})],
        })
        with self.assertRaises(ValidationError):
            order.action_confirm()

    def test_manual_approval_can_be_approved(self):
        self.route.write({"approval_policy": "manual"})
        order = self.env["sale.order"].create({
            "company_id": self.company_a.id,
            "partner_id": self.customer_b.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1, "price_unit": 100})],
        })
        order.action_confirm()
        event = self.env["ff.intercompany.event"].search([
            ("source_model", "=", "sale.order"), ("source_res_id", "=", order.id),
        ], limit=1)
        target = self.env["purchase.order"].browse(event.target_res_id)
        self.assertEqual(event.approval_status, "pending")
        self.assertEqual(target.state, "draft")
        event.with_user(self.env.ref("base.user_admin")).action_approve()
        self.assertEqual(event.approval_status, "approved")
        self.assertEqual(target.state, "purchase")
