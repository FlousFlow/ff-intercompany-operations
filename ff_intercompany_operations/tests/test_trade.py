from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError, ValidationError


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
        cls.company_b = cls.env["res.company"].create({"name": "IC Test Company B", "partner_id": cls.env["res.partner"].create({"name": "IC Test Company B"}).id, "currency_id": cls.env.ref("base.USD").id})
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
