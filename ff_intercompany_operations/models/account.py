from odoo import api, fields, models, _, Command
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
        if routes:
            return routes[:1]
        if trigger == "posting":
            return self._trade_route_for_move(move)
        return self.env["ff.intercompany.route"]

    def _trade_route_for_move(self, move):
        """Resolve accounting documents through the originating sale-to-purchase route."""
        sale_lines = move.invoice_line_ids.mapped("sale_line_ids")
        if move.move_type == "out_refund" and move.reversed_entry_id:
            sale_lines |= move.reversed_entry_id.invoice_line_ids.mapped("sale_line_ids")
        orders = sale_lines.mapped("order_id")
        if not orders:
            return self.env["ff.intercompany.route"]
        links = self.env["ff.intercompany.document.link"].sudo().search([
            ("source_model", "=", "sale.order"),
            ("source_res_id", "in", orders.ids),
            ("target_model", "=", "purchase.order"),
            ("source_company_id", "=", move.company_id.id),
            ("target_company_id", "!=", move.company_id.id),
            ("synchronization_status", "=", "active"),
        ])
        routes = links.mapped("route_id").filtered(
            lambda route: route.active
            and route.source_document == "sale.order"
            and route.target_document == "purchase.order"
            and route.agreement_id.active
            and route.agreement_id.status == "active"
            and (not route.agreement_id.effective_date or route.agreement_id.effective_date <= fields.Date.context_today(self))
            and (not route.agreement_id.expiration_date or route.agreement_id.expiration_date >= fields.Date.context_today(self))
            and route.source_company_id == move.company_id
            and (not route.source_partner_id or route.source_partner_id == move.partner_id)
            and (
                not route.agreement_id.authorized_user_ids
                or self.env.user in route.agreement_id.authorized_user_ids
            )
        )
        if len(routes) > 1:
            raise ValidationError(_(
                "The invoice lines link to more than one intercompany route. "
                "Split the credit note by agreement before posting."
            ))
        return routes[:1]

    def process_event(self, event):
        move = self.env["account.move"].browse(event.source_res_id).exists()
        if (not move or move.state != "posted" or move.company_id != event.source_company_id
                or move.move_type not in ("out_invoice", "out_refund")):
            raise ValidationError(_("The source invoice is unavailable or is not a customer invoice."))
        route = event.route_id
        if not route.destination_partner_id:
            raise ValidationError(_("The accounting route requires a destination vendor contact."))
        if move.currency_id != route.destination_company_id.currency_id:
            raise ValidationError(_("Invoice currency must match the destination company currency in this release."))
        if route.source_document == "sale.order":
            return self._process_trade_account_move(move, route)
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

    def _linked_purchase_order(self, move, route):
        source_lines = move.invoice_line_ids.mapped("sale_line_ids")
        if move.move_type == "out_refund" and move.reversed_entry_id:
            source_lines |= move.reversed_entry_id.invoice_line_ids.mapped("sale_line_ids")
        orders = source_lines.mapped("order_id")
        if not orders:
            raise ValidationError(_(
                "This intercompany invoice is not linked to a sales order line."
            ))
        links = self.env["ff.intercompany.document.link"].sudo().search([
            ("route_id", "=", route.id),
            ("source_model", "=", "sale.order"),
            ("source_res_id", "in", orders.ids),
            ("target_model", "=", "purchase.order"),
            ("synchronization_status", "=", "active"),
        ])
        purchase_orders = self.env["purchase.order"].sudo().browse(links.mapped("target_res_id"))
        purchase_orders = purchase_orders.filtered(
            lambda order: order.company_id == route.destination_company_id
        )
        if len(purchase_orders) != 1:
            raise ValidationError(_(
                "A single confirmed counterpart purchase order is required to synchronize "
                "this invoice. Found %(count)s.",
                count=len(purchase_orders),
            ))
        return purchase_orders, source_lines

    def _purchase_line_for_sale_line(self, sale_line, purchase_order):
        purchase_lines = purchase_order.order_line.filtered(
            lambda line: not line.display_type
        )
        linked = purchase_lines.filtered(
            lambda line: line.ff_intercompany_source_line_id == sale_line
        )
        if not linked:
            # Older purchase orders predate the explicit line link. Use a
            # strict product/name/UoM match and refuse ambiguous duplicates.
            linked = purchase_lines.filtered(
                lambda line: line.product_id == sale_line.product_id
                and line.name == sale_line.name
                and line.uom_id.category_id == sale_line.product_uom_id.category_id
            )
        if len(linked) != 1:
            raise ValidationError(_(
                "Could not match sales line %(line)s to exactly one line in purchase order %(order)s.",
                line=sale_line.display_name,
                order=purchase_order.display_name,
            ))
        return linked

    def _purchase_line_for_invoice_line(self, invoice_line, purchase_order):
        sale_lines = invoice_line.sale_line_ids
        if not sale_lines and invoice_line.move_id.move_type == "out_refund" and invoice_line.move_id.reversed_entry_id:
            original_lines = invoice_line.move_id.reversed_entry_id.invoice_line_ids.filtered(
                lambda line: line.product_id == invoice_line.product_id
            )
            sale_lines = original_lines.mapped("sale_line_ids")
        if len(sale_lines) != 1:
            raise ValidationError(_(
                "Invoice line %(line)s must identify exactly one source sales order line.",
                line=invoice_line.name or invoice_line.product_id.display_name,
            ))
        return self._purchase_line_for_sale_line(sale_lines, purchase_order)

    def _original_vendor_bill_lines(self, purchase_lines, destination_company):
        bill_lines = self.env["account.move.line"].sudo().search([
            ("purchase_line_id", "in", purchase_lines.ids),
            ("move_id.move_type", "=", "in_invoice"),
            ("move_id.state", "=", "posted"),
            ("move_id.company_id", "=", destination_company.id),
            ("display_type", "in", [False, "product"]),
        ])
        bills = bill_lines.mapped("move_id")
        if len(bills) != 1:
            raise ValidationError(_(
                "The linked purchase order must have one posted vendor bill before "
                "its customer credit note can be mirrored. Found %(count)s.",
                count=len(bills),
            ))
        return bills, bill_lines

    def _process_trade_account_move(self, move, route):
        purchase_order, _source_lines = self._linked_purchase_order(move, route)
        if move.move_type == "out_refund":
            return self._create_counterpart_vendor_credit(move, route, purchase_order)
        if move.sab_replacement_credit_note_id:
            return self._create_replacement_vendor_bill(move, route, purchase_order)
        raise ValidationError(_(
            "Only a posted customer credit note or a replacement invoice can use "
            "the trade route for accounting synchronization."
        ))

    def _create_counterpart_vendor_credit(self, source_credit, route, purchase_order):
        existing = self.env["account.move"].sudo().search([
            ("ff_intercompany_source_move_id", "=", source_credit.id),
            ("ff_intercompany_route_id", "=", route.id),
            ("move_type", "=", "in_refund"),
            ("state", "!=", "cancel"),
        ], limit=1)
        if existing:
            return existing
        line_map = {}
        for source_line in source_credit.invoice_line_ids.filtered(
            lambda line: line.product_id and line.display_type in (False, "product")
        ):
            purchase_line = self._purchase_line_for_invoice_line(source_line, purchase_order)
            line_map[source_line] = purchase_line
        if not line_map:
            raise ValidationError(_("The customer credit note has no product lines to mirror."))
        target_bill, target_bill_lines = self._original_vendor_bill_lines(
            self.env["purchase.order.line"].browse([line.id for line in line_map.values()]),
            route.destination_company_id,
        )
        source = source_credit.reversed_entry_id
        if source and source.move_type != "out_invoice":
            raise ValidationError(_("The customer credit note must reverse a customer invoice."))

        invoice_lines = []
        for source_line, purchase_line in line_map.items():
            bill_lines = target_bill_lines.filtered(
                lambda line: line.purchase_line_id == purchase_line
                and line.product_id == source_line.product_id
            )
            if len(bill_lines) != 1:
                raise ValidationError(_(
                    "Could not match %(product)s to one line on the destination vendor bill.",
                    product=source_line.product_id.display_name,
                ))
            bill_line = bill_lines
            source_uom = source_line.product_uom_id or source_line.product_id.uom_id
            target_uom = bill_line.product_uom_id or purchase_line.uom_id
            quantity = source_uom._compute_quantity(
                abs(source_line.quantity), target_uom, round=False
            )
            prior_credit_lines = self.env["account.move.line"].sudo().search([
                ("purchase_line_id", "=", purchase_line.id),
                ("move_id.move_type", "=", "in_refund"),
                ("move_id.state", "=", "posted"),
                ("move_id.reversed_entry_id", "=", bill_line.move_id.id),
            ])
            already_credited = sum(abs(line.quantity) for line in prior_credit_lines)
            remaining_bill_quantity = max(0.0, abs(bill_line.quantity) - already_credited)
            if target_uom.compare(quantity, remaining_bill_quantity) > 0:
                raise ValidationError(_(
                    "The requested credit quantity for %(product)s exceeds the "
                    "remaining quantity on destination vendor bill %(bill)s.",
                    product=source_line.product_id.display_name,
                    bill=bill_line.move_id.display_name,
                ))
            source_price = source_uom._compute_price(source_line.price_unit, target_uom)
            if route.pricing_policy == "document" and (
                route.destination_company_id.currency_id.compare_amounts(
                    source_price, bill_line.price_unit
                )
                or source_line.discount != bill_line.discount
            ):
                raise ValidationError(_(
                    "The source credit price for %(product)s does not match its "
                    "original destination vendor bill. Resolve the price difference "
                    "before posting the intercompany credit note.",
                    product=source_line.product_id.display_name,
                ))
            invoice_lines.append(Command.create({
                "name": bill_line.name,
                "product_id": bill_line.product_id.id,
                "product_uom_id": target_uom.id,
                # Account move type controls the debit/credit sign; quantity
                # stays positive just like a standard Odoo vendor credit note.
                "quantity": quantity,
                "price_unit": bill_line.price_unit,
                "discount": bill_line.discount,
                "account_id": bill_line.account_id.id,
                "tax_ids": [Command.set(bill_line.tax_ids.ids)],
                "purchase_line_id": purchase_line.id,
            }))

        credit = self.env["account.move"].sudo().with_company(
            route.destination_company_id
        ).create({
            "move_type": "in_refund",
            "company_id": route.destination_company_id.id,
            "partner_id": route.destination_partner_id.id,
            "currency_id": target_bill.currency_id.id,
            "journal_id": target_bill.journal_id.id,
            "invoice_date": source_credit.invoice_date,
            "invoice_origin": source_credit.name or source_credit.ref,
            "ref": source_credit.name or source_credit.ref,
            "reversed_entry_id": target_bill.id,
            "ff_intercompany_source_move_id": source_credit.id,
            "ff_intercompany_route_id": route.id,
            "invoice_line_ids": invoice_lines,
        })
        if route.approval_policy == "auto":
            credit.action_post()
        return credit

    def _create_replacement_vendor_bill(self, source_invoice, route, purchase_order):
        existing = self.env["account.move"].sudo().search([
            ("ff_intercompany_source_move_id", "=", source_invoice.id),
            ("ff_intercompany_route_id", "=", route.id),
            ("move_type", "=", "in_invoice"),
            ("state", "!=", "cancel"),
        ], limit=1)
        if existing:
            return existing
        counterpart_credit_link = self.env["ff.intercompany.document.link"].sudo().search([
            ("route_id", "=", route.id),
            ("source_model", "=", "account.move"),
            ("source_res_id", "=", source_invoice.sab_replacement_credit_note_id.id),
            ("target_model", "=", "account.move"),
            ("synchronization_status", "=", "active"),
        ], limit=1)
        if not counterpart_credit_link:
            raise ValidationError(_(
                "The replacement invoice cannot be synchronized until its credit note "
                "has a counterpart in the destination company."
            ))
        target_credit = self.env["account.move"].sudo().browse(
            counterpart_credit_link.target_res_id
        ).exists()
        if not target_credit or target_credit.move_type != "in_refund" or target_credit.state != "posted":
            raise ValidationError(_(
                "The destination vendor credit note must be posted before the "
                "replacement vendor bill."
            ))

        invoice_lines = []
        for source_line in source_invoice.invoice_line_ids.filtered(
            lambda line: line.product_id and line.display_type in (False, "product")
        ):
            purchase_line = self._purchase_line_for_invoice_line(source_line, purchase_order)
            source_uom = source_line.product_uom_id or source_line.product_id.uom_id
            target_uom = purchase_line.uom_id
            quantity = source_uom._compute_quantity(
                abs(source_line.quantity), target_uom, round=False
            )
            price_unit = source_uom._compute_price(source_line.price_unit, target_uom)
            line_values = purchase_line.with_company(route.destination_company_id)._prepare_account_move_line()
            line_values.update({
                "quantity": quantity,
                "product_uom_id": target_uom.id,
                "price_unit": price_unit,
                "discount": source_line.discount,
                "purchase_line_id": purchase_line.id,
            })
            invoice_lines.append(Command.create(line_values))
        if not invoice_lines:
            raise ValidationError(_("The replacement invoice has no product lines to mirror."))

        bill = self.env["account.move"].sudo().with_company(
            route.destination_company_id
        ).create({
            "move_type": "in_invoice",
            "company_id": route.destination_company_id.id,
            "partner_id": route.destination_partner_id.id,
            "currency_id": source_invoice.currency_id.id,
            "invoice_date": source_invoice.invoice_date,
            "invoice_origin": source_invoice.name or source_invoice.ref,
            "ref": source_invoice.name or source_invoice.ref,
            "ff_intercompany_source_move_id": source_invoice.id,
            "ff_intercompany_route_id": route.id,
            "invoice_line_ids": invoice_lines,
        })
        if route.approval_policy == "auto":
            bill.action_post()
        return bill


class IntercompanyEventProcessor(models.AbstractModel):
    _inherit = "ff.intercompany.event.service"

    def _process_event(self, event):
        if event.source_model == "account.move":
            return self.env["ff.intercompany.account.service"].process_event(event)
        return super()._process_event(event)


class AccountMove(models.Model):
    _inherit = "account.move"

    ff_intercompany_source_move_id = fields.Many2one(
        "account.move", string="Intercompany Source Document", copy=False, index=True
    )
    ff_intercompany_route_id = fields.Many2one(
        "ff.intercompany.route", string="Intercompany Route", copy=False, index=True
    )

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
        for move in self.filtered(
            lambda m: m.state == "posted" and m.move_type in ("out_invoice", "out_refund")
        ):
            route = self.env["ff.intercompany.account.service"]._route_for(move)
            if not route:
                continue
            if (
                route.source_document == "sale.order"
                and move.move_type == "out_invoice"
                and not move.sab_replacement_credit_note_id
            ):
                # The sale-to-purchase route already created the supplier bill
                # from its purchase order. Avoid posting a duplicate bill here.
                continue
            existing_counterpart = self.env["account.move"].sudo().search([
                ("ff_intercompany_source_move_id", "=", move.id),
                ("ff_intercompany_route_id", "=", route.id),
                ("move_type", "=", "in_refund" if move.move_type == "out_refund" else "in_invoice"),
                ("state", "!=", "cancel"),
            ], limit=1)
            if existing_counterpart:
                event = event_model.create_for_document(route, move, "posting", target_model="account.move")
                event.write({"target_res_id": existing_counterpart.id, "state": "completed"})
                self.env["ff.intercompany.document.link"].sudo().create_for_event(event, existing_counterpart)
                continue
            event = event_model.create_for_document(route, move, "posting", target_model="account.move")
            if event.state != "completed":
                service.process(event)
            if move.move_type == "out_refund" and move.sab_created_from_stock_return:
                source_returns = self.env["stock.picking"].sudo().search([
                    ("auto_account_move_id", "=", move.id),
                    ("state", "=", "done"),
                    ("picking_type_code", "=", "incoming"),
                ])
                for source_return in source_returns:
                    source_return._ff_sync_intercompany_return()
        return result

    def button_draft(self):
        if self.env.context.get("ff_intercompany_skip_draft_guard"):
            return super().button_draft()
        Link = self.env["ff.intercompany.document.link"].sudo()
        Event = self.env["ff.intercompany.event"].sudo()
        for move in self.filtered(lambda record: record.state == "posted"):
            if move.move_type == "out_invoice":
                route = self.env["ff.intercompany.account.service"]._trade_route_for_move(move)
                if route:
                    purchase_order, source_lines = self.env[
                        "ff.intercompany.account.service"
                    ]._linked_purchase_order(move, route)
                    purchase_lines = purchase_order.order_line.filtered(
                        lambda line: line.ff_intercompany_source_line_id in source_lines
                    )
                    posted_target_bill_line = self.env["account.move.line"].sudo().search([
                        ("purchase_line_id", "in", purchase_lines.ids),
                        ("move_id.move_type", "=", "in_invoice"),
                        ("move_id.state", "in", ["draft", "posted"]),
                        ("move_id.company_id", "=", route.destination_company_id.id),
                    ], limit=1)
                    if posted_target_bill_line:
                        raise UserError(_(
                            "This sales invoice has a posted supplier bill in the other "
                            "company (%s). Correct it with a credit note or reversal before "
                            "resetting the source invoice to draft.",
                            posted_target_bill_line.move_id.display_name,
                        ))
            links = Link.search([
                ("source_model", "=", "account.move"),
                ("source_res_id", "=", move.id),
                ("target_model", "=", "account.move"),
                ("synchronization_status", "=", "active"),
            ])
            for link in links:
                counterpart = self.env["account.move"].sudo().browse(link.target_res_id).exists()
                if not counterpart:
                    continue
                if counterpart.state == "posted":
                    raise UserError(_(
                        "This document has already been posted in the other company (%s). "
                        "It cannot be reset to draft independently. Reverse or correct "
                        "the intercompany counterpart first.",
                        counterpart.display_name,
                    ))
                linked_returns = self.env["stock.picking"].sudo().search([
                    ("sab_credit_note_id", "=", counterpart.id),
                    ("state", "!=", "cancel"),
                ]) | self.env["stock.picking"].sudo().search([
                    ("sab_replacement_invoice_id", "=", counterpart.id),
                    ("state", "!=", "cancel"),
                ])
                if linked_returns.filtered(lambda picking: picking.state == "done"):
                    raise UserError(_(
                        "A completed intercompany stock return is linked to %s. "
                        "Create a correcting stock operation before resetting this document.",
                        counterpart.display_name,
                    ))
                linked_returns.action_cancel()
                if counterpart.state == "draft":
                    counterpart.button_cancel()
                link.write({"synchronization_status": "cancelled"})
                if link.event_id:
                    Event.browse(link.event_id.id).write({"state": "cancelled"})
        return super().button_draft()

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
