from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError, UserError


class IntercompanyAccountService(models.AbstractModel):
    _name = "ff.intercompany.account.service"
    _description = "Intercompany Accounting Service"

    def _route_for(self, move, trigger="posting"):
        today = fields.Date.context_today(self)
        routes = self.env["ff.intercompany.route"].sudo().search([
            ("active", "=", True),
            ("source_company_id", "=", move.company_id.id),
            ("source_document", "=", "account.move"),
            ("target_document", "=", "account.move"),
            ("trigger", "=", trigger),
            ("agreement_id.status", "=", "active"),
            ("agreement_id.active", "=", True),
            ("agreement_id.effective_date", "<=", today),
            "|", ("agreement_id.expiration_date", "=", False),
            ("agreement_id.expiration_date", ">=", today),
        ], order="sequence,id")
        authorized = routes.filtered(
            lambda route: not route.agreement_id.authorized_user_ids
            or self.env.user in route.agreement_id.authorized_user_ids
        )
        if routes and not authorized:
            raise ValidationError(_(
                "You are not authorized to run this intercompany agreement."
            ))
        routes = authorized
        specific = routes.filtered(lambda route: route.source_partner_id == move.partner_id)
        if specific:
            routes = specific
        else:
            routes = routes.filtered(lambda route: not route.source_partner_id)
        if len(routes) > 1:
            raise ValidationError(_(
                "Multiple active intercompany routes match %(document)s. "
                "Set a unique partner route."
            ) % {"document": move.display_name})
        return routes[:1]

    def process_event(self, event):
        move = self.env["account.move"].browse(event.source_res_id).exists()
        if not move or move.company_id != event.source_company_id or move.move_type not in ("out_invoice", "out_refund"):
            raise ValidationError(_("The source invoice is unavailable or is not a customer invoice."))
        route = event.route_id
        if not route.destination_partner_id:
            raise ValidationError(_("The accounting route requires a destination vendor contact."))
        if move.currency_id != route.destination_company_id.currency_id:
            raise ValidationError(_("Invoice currency must match the destination company currency in this release."))
        values = {
            "move_type": "in_refund" if move.move_type == "out_refund" else "in_invoice",
            "company_id": route.destination_company_id.id,
            "partner_id": route.destination_partner_id.id,
            "currency_id": move.currency_id.id,
            "invoice_date": move.invoice_date,
            "invoice_origin": _("Intercompany from %s") % (move.name or move.ref),
            "ref": move.name or move.ref,
            "invoice_line_ids": [],
        }
        target_model = self.env["account.move"].sudo().with_company(route.destination_company_id)
        for line in move.invoice_line_ids.filtered(lambda l: not l.display_type):
            product = line.product_id
            if product and product.company_id and product.company_id != route.destination_company_id:
                raise ValidationError(_("Product %s is not shared with the destination company.") % product.display_name)
            price_unit = line.price_unit
            if route.pricing_policy == "agreed" and product:
                price_unit = product.with_company(route.destination_company_id).lst_price
            values["invoice_line_ids"].append((0, 0, {
                "product_id": product.id if product else False,
                "name": line.name,
                "quantity": line.quantity,
                "product_uom_id": line.product_uom_id.id if line.product_uom_id else False,
                "price_unit": price_unit,
                "discount": line.discount,
                "tax_ids": [(6, 0, [])],
            }))
        bill = target_model.create(values)
        if route.approval_policy == "auto":
            # Explicitly allowed only for the accounting document; no payment
            # or reconciliation is performed by this addon.
            bill.action_post()
        return bill


class IntercompanyEventProcessor(models.AbstractModel):
    _inherit = "ff.intercompany.event.service"

    def _process_event(self, event):
        if event.route_id.source_document == "account.move":
            return self.env["ff.intercompany.account.service"].process_event(event)
        return super()._process_event(event)


class AccountMove(models.Model):
    _inherit = "account.move"

    intercompany_sync_state = fields.Selection([
        ("none", "Not Intercompany"), ("pending", "Pending"), ("completed", "Counterpart Created"),
        ("failed", "Failed"), ("blocked", "Blocked"),
    ], compute="_compute_intercompany_status", string="Intercompany Status")
    intercompany_counterpart_ref = fields.Char(compute="_compute_intercompany_status", string="Counterpart")
    is_intercompany_document = fields.Boolean(compute="_compute_intercompany_status")

    def _compute_intercompany_status(self):
        link_model = self.env["ff.intercompany.document.link"].sudo()
        event_model = self.env["ff.intercompany.event"].sudo()
        for move in self:
            link = link_model.search([("source_model", "=", "account.move"), ("source_res_id", "=", move.id)], limit=1)
            event = event_model.search([("source_model", "=", "account.move"), ("source_res_id", "=", move.id)], order="id desc", limit=1)
            move.is_intercompany_document = bool(link or event)
            move.intercompany_sync_state = event.state if event else "none"
            move.intercompany_counterpart_ref = "%s/%s" % (link.target_model, link.target_res_id) if link else False

    def action_view_intercompany_status(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Intercompany Links"),
            "res_model": "ff.intercompany.document.link",
            "view_mode": "list,form",
            "domain": [("source_model", "=", "account.move"), ("source_res_id", "=", self.id)],
        }

    def action_post(self):
        result = super().action_post()
        if self.env.context.get("ff_intercompany_skip_sync"):
            return result
        event_model = self.env["ff.intercompany.event"].sudo()
        service = self.env["ff.intercompany.event.service"].sudo()
        for move in self.filtered(lambda m: m.state == "posted" and m.move_type in ("out_invoice", "out_refund")):
            route = self.env["ff.intercompany.account.service"]._route_for(move)
            if not route:
                continue
            event = event_model.create_for_document(route, move, "posting")
            if event.state != "completed":
                service.process(event)
        return result

    def action_intercompany_sync(self):
        if not self.env.user.has_group("ff_intercompany_operations.group_intercompany_manager"):
            raise AccessError(_("Only intercompany managers can synchronize manually."))
        for move in self:
            if move.move_type not in ("out_invoice", "out_refund"):
                raise UserError(_("Manual intercompany synchronization supports customer invoices and credit notes only."))
            route = self.env["ff.intercompany.account.service"]._route_for(move, trigger="manual")
            if not route:
                raise UserError(_("No manual intercompany route matches this invoice."))
            event = self.env["ff.intercompany.event"].sudo().create_for_document(route, move, "manual")
            self.env["ff.intercompany.event.service"].sudo().process(event)
        return True
