from collections import defaultdict

from odoo import _, fields, models
from odoo.exceptions import UserError, ValidationError


class StockPicking(models.Model):
    _inherit = "stock.picking"

    ff_intercompany_source_picking_id = fields.Many2one(
        "stock.picking",
        string="Intercompany Source Return",
        copy=False,
        index=True,
    )
    ff_intercompany_counterpart_picking_id = fields.Many2one(
        "stock.picking",
        string="Intercompany Counterpart Return",
        copy=False,
        index=True,
    )

    def _auto_billing_process_document(self):
        parent = getattr(super(), "_auto_billing_process_document", None)
        result = parent() if parent else True
        for picking in self:
            if (
                picking.state == "done"
                and picking.picking_type_code == "incoming"
                and picking.move_ids.filtered(
                    lambda move: move.origin_returned_move_id
                    and move.origin_returned_move_id.sale_line_id
                )
            ):
                picking._ff_sync_intercompany_return()
        return result

    def _ff_sync_intercompany_return(self):
        self.ensure_one()
        if self.ff_intercompany_counterpart_picking_id:
            if self.ff_intercompany_counterpart_picking_id.state != "cancel":
                return self.ff_intercompany_counterpart_picking_id
            self.ff_intercompany_counterpart_picking_id = False
        if self.state != "done" or self.picking_type_code != "incoming":
            return self.env["stock.picking"]

        sab_fields = self.env["stock.picking"]._fields
        credit_note = self.auto_account_move_id
        if "sab_credit_note_id" in sab_fields and self.sab_credit_note_id:
            credit_note = self.sab_credit_note_id
        if not credit_note or credit_note.state != "posted":
            return self.env["stock.picking"]

        account_service = self.env["ff.intercompany.account.service"].sudo()
        route = account_service._trade_route_for_move(credit_note)
        if not route:
            return self.env["stock.picking"]
        if route.approval_policy != "auto":
            return self.env["stock.picking"]

        credit_link = self.env["ff.intercompany.document.link"].sudo().search([
            ("route_id", "=", route.id),
            ("source_model", "=", "account.move"),
            ("source_res_id", "=", credit_note.id),
            ("target_model", "=", "account.move"),
            ("synchronization_status", "=", "active"),
        ], limit=1)
        if not credit_link:
            raise ValidationError(_(
                "The counterpart vendor credit note must be created before the "
                "intercompany stock return can be completed."
            ))
        target_credit = self.env["account.move"].sudo().browse(
            credit_link.target_res_id
        ).exists()
        if not target_credit or target_credit.move_type != "in_refund" or target_credit.state != "posted":
            raise ValidationError(_(
                "The counterpart vendor credit note is missing or not posted."
            ))

        purchase_order, _source_lines = account_service._linked_purchase_order(
            credit_note, route
        )
        allocations = defaultdict(float)
        for source_move in self.move_ids.filtered(
            lambda move: move.state == "done"
            and move.origin_returned_move_id
            and move.origin_returned_move_id.sale_line_id
        ):
            sale_line = source_move.origin_returned_move_id.sale_line_id
            purchase_line = account_service._purchase_line_for_sale_line(
                sale_line, purchase_order
            )
            if purchase_line.product_id.tracking:
                raise UserError(_(
                    "The intercompany return for tracked product %s requires manual "
                    "lot or serial mapping.",
                    purchase_line.product_id.display_name,
                ))
            returned_quantity = source_move.uom_id._compute_quantity(
                source_move.quantity, sale_line.product_uom_id, round=False
            )
            returned_quantity = sale_line.product_uom_id._compute_quantity(
                returned_quantity, purchase_line.uom_id, round=False
            )
            receipt_moves = purchase_line.move_ids.filtered(
                lambda move: move.state == "done"
                and not move.origin_returned_move_id
                and move.picking_id.state == "done"
                and move.picking_id.picking_type_code == "incoming"
            ).sorted(lambda move: (move.date, move.id))
            remaining = returned_quantity
            for receipt_move in receipt_moves:
                original_quantity = receipt_move.uom_id._compute_quantity(
                    receipt_move.quantity, purchase_line.uom_id, round=False
                )
                previously_returned = sum(
                    return_move.uom_id._compute_quantity(
                        return_move.quantity, purchase_line.uom_id, round=False
                    )
                    for return_move in receipt_move.returned_move_ids
                    if return_move.state == "done"
                )
                available = max(0.0, original_quantity - previously_returned)
                take = min(remaining, available)
                if purchase_line.uom_id.is_zero(take):
                    continue
                allocations[receipt_move] += purchase_line.uom_id._compute_quantity(
                    take, receipt_move.uom_id, round=False
                )
                remaining -= take
                if purchase_line.uom_id.is_zero(remaining):
                    break
            if not purchase_line.uom_id.is_zero(remaining):
                raise ValidationError(_(
                    "The returned quantity for %(product)s exceeds the remaining "
                    "received quantity in purchase order %(order)s.",
                    product=purchase_line.product_id.display_name,
                    order=purchase_order.display_name,
                ))

        if not allocations:
            return self.env["stock.picking"]

        replacement_bill = self.env["account.move"]
        if "sab_replacement_invoice_id" in sab_fields and self.sab_replacement_invoice_id:
            replacement_link = self.env["ff.intercompany.document.link"].sudo().search([
                ("route_id", "=", route.id),
                ("source_model", "=", "account.move"),
                ("source_res_id", "=", self.sab_replacement_invoice_id.id),
                ("target_model", "=", "account.move"),
                ("synchronization_status", "=", "active"),
            ], limit=1)
            if replacement_link:
                replacement_bill = self.env["account.move"].sudo().browse(
                    replacement_link.target_res_id
                ).exists()

        allocations_by_receipt = defaultdict(dict)
        for receipt_move, quantity in allocations.items():
            allocations_by_receipt[receipt_move.picking_id][receipt_move] = quantity

        counterpart_pickings = self.env["stock.picking"].sudo().browse()
        for receipt, receipt_allocations in allocations_by_receipt.items():
            existing = self.env["stock.picking"].sudo().search([
                ("ff_intercompany_source_picking_id", "=", self.id),
                ("return_id", "=", receipt.id),
                ("state", "!=", "cancel"),
            ], limit=1)
            if existing:
                counterpart_pickings |= existing
                continue

            target_return = receipt.sudo()._create_return()
            for return_move in target_return.move_ids:
                source_receipt_move = return_move.origin_returned_move_id
                quantity = receipt_allocations.get(source_receipt_move)
                if not quantity:
                    return_move.unlink()
                    continue
                return_move.product_uom_qty = quantity
            target_return.write({
                "origin": _("Intercompany return of %(source)s", source=self.name),
                "ff_intercompany_source_picking_id": self.id,
                "sab_credit_note_id": target_credit.id,
                "sab_replacement_invoice_id": replacement_bill.id or False,
            })
            target_return.action_confirm()
            target_return.action_assign()
            active_moves = target_return.move_ids.filtered(
                lambda move: move.state not in ("done", "cancel")
            )
            if any(move.state != "assigned" for move in active_moves):
                raise UserError(_(
                    "The counterpart stock return %(picking)s could not reserve the "
                    "full quantity. The source return is done; review stock before retrying.",
                    picking=target_return.name,
                ))
            for move in active_moves:
                move.quantity = move.product_uom_qty
            result = target_return.with_context(
                skip_backorder=True,
                skip_zero_demand_check=True,
            ).button_validate()
            if isinstance(result, dict) or target_return.state != "done":
                raise UserError(_(
                    "Odoo requires manual validation for counterpart return %s.",
                    target_return.name,
                ))
            counterpart_pickings |= target_return

        if counterpart_pickings:
            self.ff_intercompany_counterpart_picking_id = counterpart_pickings[:1]
        return counterpart_pickings
