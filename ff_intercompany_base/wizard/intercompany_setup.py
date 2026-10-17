from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class IntercompanySetupWizard(models.TransientModel):
    _name = "ff.intercompany.setup.wizard"
    _description = "Intercompany Quick Setup"

    name = fields.Char(
        string="Setup Name",
        required=True,
        default=lambda self: _("Intercompany Setup"),
        help="A clear name for this company-to-company synchronization.",
    )
    edit_contacts = fields.Boolean(
        string="Edit Contact Mapping Manually",
        help="Enable this only when the automatically detected company contacts are not correct.",
    )
    advanced_settings = fields.Boolean(
        string="Show Advanced Settings",
        help="Show trigger and pricing options that are normally safe to leave at their defaults.",
    )
    company_a_id = fields.Many2one(
        "res.company", string="Source Company", required=True,
        default=lambda self: self.env.company,
    )
    company_b_id = fields.Many2one(
        "res.company", string="Destination Company", required=True,
    )
    partner_a_id = fields.Many2one(
        "res.partner", string="Counterparty Contact in Source Company", required=True,
        help="The destination company's contact record used in the source company.",
    )
    partner_b_id = fields.Many2one(
        "res.partner", string="Counterparty Contact in Destination Company", required=True,
        help="The source company's contact record used in the destination company.",
    )
    direction = fields.Selection([
        ("sale_to_purchase", "Sales Order → Purchase Order"),
        ("purchase_to_sale", "Purchase Order → Sales Order"),
    ], string="Synchronization Direction", required=True, default="sale_to_purchase",
        help="Choose which document starts the synchronization and which document is created in the other company.")
    create_reverse = fields.Boolean(
        string="Create Reverse Direction Too",
        help="Also create the opposite route using the same company and contact mapping.",
    )
    trigger = fields.Selection([
        ("confirmation", "When the source document is confirmed"),
        ("manual", "Manual only"),
    ], string="When to Synchronize", required=True, default="confirmation",
        help="Automatic creates the counterpart after confirmation; manual waits for a manager retry.")
    approval_policy = fields.Selection([
        ("draft", "Create a Draft"),
        ("manual", "Create and Await Approval"),
        ("auto", "Confirm Automatically"),
    ], string="Destination Document", required=True, default="draft",
        help="Draft is safest. Manual waits for approval. Automatic confirms the destination document immediately.")
    pricing_policy = fields.Selection([
        ("document", "Use Source Document Price"),
        ("agreed", "Use Agreed Commercial Price"),
    ], string="Pricing", required=True, default="document",
        help="Choose whether to copy the source price or use the agreed commercial price for the route.")

    @api.onchange("company_a_id")
    def _onchange_company_a_id(self):
        if self.company_a_id:
            self.partner_b_id = self.company_a_id.partner_id
        if self.company_a_id == self.company_b_id:
            self.company_b_id = False
            self.partner_a_id = False

    @api.onchange("company_b_id")
    def _onchange_company_b_id(self):
        if self.company_b_id:
            self.partner_a_id = self.company_b_id.partner_id

    def _validate(self):
        self.ensure_one()
        if self.company_a_id == self.company_b_id:
            raise ValidationError(_("Choose two different companies."))
        for partner, company in (
            (self.partner_a_id, self.company_a_id),
            (self.partner_b_id, self.company_b_id),
        ):
            if partner.company_id and partner.company_id != company:
                raise ValidationError(_(
                    "Contact %(contact)s belongs to %(actual)s, not %(expected)s.",
                    contact=partner.display_name,
                    actual=partner.company_id.display_name,
                    expected=company.display_name,
                ))

    def _route_values(self, agreement, mapping, source_company, destination_company,
                      source_partner, destination_partner, direction):
        if direction == "sale_to_purchase":
            source_document, target_document = "sale.order", "purchase.order"
            label = _("Sales Order → Purchase Order")
        else:
            source_document, target_document = "purchase.order", "sale.order"
            label = _("Purchase Order → Sales Order")
        return {
            "name": "%s: %s" % (self.name, label),
            "agreement_id": agreement.id,
            "counterparty_mapping_id": mapping.id,
            "source_company_id": source_company.id,
            "destination_company_id": destination_company.id,
            "source_document": source_document,
            "target_document": target_document,
            "trigger": self.trigger,
            "approval_policy": self.approval_policy,
            "pricing_policy": self.pricing_policy,
            "source_partner_id": source_partner.id,
            "destination_partner_id": destination_partner.id,
            "active": True,
        }

    def _create_route_if_missing(self, values):
        Route = self.env["ff.intercompany.route"]
        route = Route.search([
            ("agreement_id", "=", values["agreement_id"]),
            ("source_company_id", "=", values["source_company_id"]),
            ("destination_company_id", "=", values["destination_company_id"]),
            ("source_document", "=", values["source_document"]),
            ("target_document", "=", values["target_document"]),
        ], limit=1)
        if route:
            route.write(values)
            return route
        return Route.create(values)

    def action_create_setup(self):
        self.ensure_one()
        self._validate()
        Agreement = self.env["ff.intercompany.agreement"]
        agreement = Agreement.search([
            ("company_pair_key", "=", ":".join(map(str, sorted((self.company_a_id.id, self.company_b_id.id))))),
            ("active", "=", True),
        ], limit=1)
        if not agreement:
            agreement = Agreement.create({
                "name": self.name,
                "company_a_id": self.company_a_id.id,
                "company_b_id": self.company_b_id.id,
                "status": "active",
                "active": True,
                "default_trigger": self.trigger,
                "default_approval": self.approval_policy,
                "default_pricing": self.pricing_policy,
                "authorized_user_ids": [(4, self.env.user.id)],
            })
        else:
            agreement.write({
                "active": True,
                "status": "active",
                "default_trigger": self.trigger,
                "default_approval": self.approval_policy,
                "default_pricing": self.pricing_policy,
            })

        Mapping = self.env["ff.intercompany.counterparty"]
        mapping = Mapping.search([
            ("agreement_id", "=", agreement.id),
            ("partner_a_id", "=", self.partner_a_id.id),
            ("partner_b_id", "=", self.partner_b_id.id),
        ], limit=1)
        if not mapping:
            mapping = Mapping.create({
                "name": "%s: %s ↔ %s" % (
                    self.name, self.partner_a_id.display_name, self.partner_b_id.display_name),
                "agreement_id": agreement.id,
                "company_a_id": self.company_a_id.id,
                "company_b_id": self.company_b_id.id,
                "partner_a_id": self.partner_a_id.id,
                "partner_b_id": self.partner_b_id.id,
                "role": "trade",
                "active": True,
            })

        if self.direction == "sale_to_purchase":
            reverse_direction = "purchase_to_sale"
        else:
            reverse_direction = "sale_to_purchase"
        self._create_route_if_missing(self._route_values(
            agreement, mapping, self.company_a_id, self.company_b_id,
            self.partner_a_id, self.partner_b_id, self.direction,
        ))
        if self.create_reverse:
            self._create_route_if_missing(self._route_values(
                agreement, mapping, self.company_b_id, self.company_a_id,
                self.partner_b_id, self.partner_a_id, reverse_direction,
            ))

        return {
            "type": "ir.actions.act_window",
            "name": _("Intercompany Setup Complete"),
            "res_model": "ff.intercompany.agreement",
            "res_id": agreement.id,
            "view_mode": "form",
            "target": "current",
        }
