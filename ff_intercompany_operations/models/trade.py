from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError


class IntercompanyTradeService(models.AbstractModel):
    _name = "ff.intercompany.trade.service"
    _description = "Intercompany Trade Service"

    def _route_for(self, source, source_document, target_document):
        # Route configuration is privileged metadata; only the matching route
        # is returned, while source documents remain in the caller's company
        # context.
        return self.env["ff.intercompany.route"].sudo().search([
            ("active", "=", True),
            ("source_company_id", "=", source.company_id.id),
            ("destination_company_id", "!=", source.company_id.id),
            ("source_document", "=", source_document),
            ("target_document", "=", target_document),
            ("trigger", "=", "confirmation"),
            ("agreement_id.status", "=", "active"),
            ("agreement_id.active", "=", True),
        ], limit=1)

    def _get_product(self, line, target_company):
        product = line.product_id
        if not product:
            return product
        if product.company_id and product.company_id != target_company:
            raise ValidationError(_("Product %s is company-specific and cannot be shared across this route.") % product.display_name)
        return product

    def _target_model(self, model_name, route):
        model = self.env[model_name]
        if route.integration_user_id:
            model = model.with_user(route.integration_user_id)
        return model.with_company(route.destination_company_id)

    def _destination_warehouse(self, route):
        """Return the configured warehouse or the company's first warehouse.

        Odoo 20 requires an explicit picking type when creating a purchase
        order, and the sales order warehouse must belong to the destination
        company.  Quick Setup intentionally leaves warehouses optional, so
        use the standard warehouse for that company as a safe default.
        """
        warehouse = route.destination_warehouse_id
        if not warehouse:
            warehouse = self.env["stock.warehouse"].sudo().search([
                ("company_id", "=", route.destination_company_id.id),
            ], order="id", limit=1)
        if not warehouse:
            raise ValidationError(_(
                "No warehouse is configured for destination company %s."
            ) % route.destination_company_id.display_name)
        return warehouse

    def process_event(self, event):
        event.ensure_one()
        if event.source_model == "sale.order" and event.target_model == "purchase.order":
            return self._sale_to_purchase(event)
        if event.source_model == "purchase.order" and event.target_model == "sale.order":
            return self._purchase_to_sale(event)
        raise UserError(_("Unsupported trade route: %s → %s") % (event.source_model, event.target_model))

    def _apply_approval_policy(self, event, target):
        if event.route_id.approval_policy != "auto":
            return target
        target = target.with_context(ff_intercompany_skip_sync=True)
        if target._name == "purchase.order":
            target.button_confirm()
        elif target._name == "sale.order":
            target.action_confirm()
        return target

    def _prepare_po_vals(self, order, route):
        if not route.destination_partner_id:
            raise ValidationError(_("The route requires a destination vendor partner."))
        vals = {
            "partner_id": route.destination_partner_id.id,
            "company_id": route.destination_company_id.id,
            "origin": _("Intercompany from %s") % order.name,
            "note": order.note,
            "date_order": order.date_order,
            "order_line": [],
        }
        warehouse = self._destination_warehouse(route)
        if not warehouse.in_type_id:
            raise ValidationError(_(
                "Warehouse %s has no incoming operation type."
            ) % warehouse.display_name)
        vals["picking_type_id"] = warehouse.in_type_id.id
        for line in order.order_line.filtered(lambda l: not l.display_type):
            product = self._get_product(line, route.destination_company_id)
            if not product:
                raise ValidationError(_("A trade line is missing a product."))
            vals["order_line"].append((0, 0, {
                "product_id": product.id,
                "name": line.name,
                "product_qty": line.product_uom_qty,
                "uom_id": line.product_uom_id.id,
                "price_unit": line.price_unit,
                "date_planned": order.date_order,
                "tax_ids": [(6, 0, [])],
            }))
        return vals

    def _prepare_so_vals(self, order, route):
        if not route.destination_partner_id:
            raise ValidationError(_("The route requires a destination customer partner."))
        vals = {
            "partner_id": route.destination_partner_id.id,
            "company_id": route.destination_company_id.id,
            "client_order_ref": _("Intercompany from %s") % order.name,
            "date_order": order.date_order,
            "order_line": [],
        }
        warehouse = self._destination_warehouse(route)
        if not warehouse.out_type_id:
            raise ValidationError(_(
                "Warehouse %s has no outgoing operation type."
            ) % warehouse.display_name)
        vals["warehouse_id"] = warehouse.id
        for line in order.order_line.filtered(lambda l: not l.display_type):
            product = self._get_product(line, route.destination_company_id)
            if not product:
                raise ValidationError(_("A trade line is missing a product."))
            vals["order_line"].append((0, 0, {
                "product_id": product.id,
                "name": line.name,
                "product_uom_qty": line.product_qty,
                "product_uom_id": line.uom_id.id,
                "price_unit": line.price_unit,
                "discount": line.discount,
            }))
        return vals

    def _sale_to_purchase(self, event):
        order = self.env["sale.order"].browse(event.source_res_id).exists()
        if not order or order.company_id != event.source_company_id:
            raise ValidationError(_("The source sales order is unavailable or belongs to another company."))
        route = event.route_id
        vals = self._prepare_po_vals(order, route)
        target = self._target_model("purchase.order", route).create(vals)
        return self._apply_approval_policy(event, target)

    def _purchase_to_sale(self, event):
        order = self.env["purchase.order"].browse(event.source_res_id).exists()
        if not order or order.company_id != event.source_company_id:
            raise ValidationError(_("The source purchase order is unavailable or belongs to another company."))
        route = event.route_id
        vals = self._prepare_so_vals(order, route)
        target = self._target_model("sale.order", route).create(vals)
        return self._apply_approval_policy(event, target)


class IntercompanyEventProcessor(models.AbstractModel):
    _inherit = "ff.intercompany.event.service"

    def _process_event(self, event):
        if event.route_id.source_document in ("sale.order", "purchase.order"):
            return self.env["ff.intercompany.trade.service"].process_event(event)
        return super()._process_event(event)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    intercompany_sync_state = fields.Selection([
        ("none", "Not Intercompany"), ("pending", "Pending"), ("completed", "Counterpart Created"),
        ("failed", "Failed"), ("blocked", "Blocked"),
    ], compute="_compute_intercompany_status", string="Intercompany Status")
    intercompany_counterpart_ref = fields.Char(compute="_compute_intercompany_status", string="Counterpart")
    is_intercompany_document = fields.Boolean(compute="_compute_intercompany_status")

    def _compute_intercompany_status(self):
        link_model = self.env["ff.intercompany.document.link"].sudo()
        event_model = self.env["ff.intercompany.event"].sudo()
        for order in self:
            link = link_model.search([("source_model", "=", "sale.order"), ("source_res_id", "=", order.id)], limit=1)
            event = event_model.search([("source_model", "=", "sale.order"), ("source_res_id", "=", order.id)], order="id desc", limit=1)
            order.is_intercompany_document = bool(link or event)
            order.intercompany_sync_state = event.state if event else "none"
            order.intercompany_counterpart_ref = "%s/%s" % (link.target_model, link.target_res_id) if link else False

    def action_view_intercompany_status(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Intercompany Links"),
            "res_model": "ff.intercompany.document.link",
            "view_mode": "list,form",
            "domain": [("source_model", "=", "sale.order"), ("source_res_id", "=", self.id)],
        }

    def action_confirm(self):
        result = super().action_confirm()
        if self.env.context.get("ff_intercompany_skip_sync"):
            return result
        service = self.env["ff.intercompany.event.service"]
        for order in self:
            route = self.env["ff.intercompany.trade.service"]._route_for(order, "sale.order", "purchase.order")
            if not route:
                continue
            # Durable event creation and state updates are the narrowly scoped
            # technical operation; route resolution above is company-bound.
            event = self.env["ff.intercompany.event"].sudo().create_for_document(route, order, "confirmation")
            if event.state != "completed":
                service.sudo().process(event)
        return result


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    intercompany_sync_state = fields.Selection([
        ("none", "Not Intercompany"), ("pending", "Pending"), ("completed", "Counterpart Created"),
        ("failed", "Failed"), ("blocked", "Blocked"),
    ], compute="_compute_intercompany_status", string="Intercompany Status")
    intercompany_counterpart_ref = fields.Char(compute="_compute_intercompany_status", string="Counterpart")
    is_intercompany_document = fields.Boolean(compute="_compute_intercompany_status")

    def _compute_intercompany_status(self):
        link_model = self.env["ff.intercompany.document.link"].sudo()
        event_model = self.env["ff.intercompany.event"].sudo()
        for order in self:
            link = link_model.search([("source_model", "=", "purchase.order"), ("source_res_id", "=", order.id)], limit=1)
            event = event_model.search([("source_model", "=", "purchase.order"), ("source_res_id", "=", order.id)], order="id desc", limit=1)
            order.is_intercompany_document = bool(link or event)
            order.intercompany_sync_state = event.state if event else "none"
            order.intercompany_counterpart_ref = "%s/%s" % (link.target_model, link.target_res_id) if link else False

    def action_view_intercompany_status(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Intercompany Links"),
            "res_model": "ff.intercompany.document.link",
            "view_mode": "list,form",
            "domain": [("source_model", "=", "purchase.order"), ("source_res_id", "=", self.id)],
        }

    def button_confirm(self):
        result = super().button_confirm()
        if self.env.context.get("ff_intercompany_skip_sync"):
            return result
        service = self.env["ff.intercompany.event.service"]
        for order in self:
            if order.state != "purchase":
                continue
            route = self.env["ff.intercompany.trade.service"]._route_for(order, "purchase.order", "sale.order")
            if not route:
                continue
            event = self.env["ff.intercompany.event"].sudo().create_for_document(route, order, "confirmation")
            if event.state != "completed":
                service.sudo().process(event)
        return result
