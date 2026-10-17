import hashlib
import json
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class IntercompanyAgreement(models.Model):
    _name = "ff.intercompany.agreement"
    _description = "Intercompany Agreement"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "name"

    name = fields.Char(required=True, tracking=True)
    company_a_id = fields.Many2one("res.company", required=True, ondelete="restrict", string="Company A")
    company_b_id = fields.Many2one("res.company", required=True, ondelete="restrict", string="Company B")
    active = fields.Boolean(default=True, tracking=True)
    effective_date = fields.Date(default=fields.Date.context_today)
    expiration_date = fields.Date()
    status = fields.Selection([("draft", "Draft"), ("active", "Active"), ("expired", "Expired")], default="draft", required=True, tracking=True)
    default_trigger = fields.Selection([("confirmation", "On Confirmation"), ("posting", "On Posting"), ("manual", "Manual")], default="confirmation", required=True)
    default_approval = fields.Selection([("draft", "Create Draft"), ("auto", "Auto Confirm"), ("manual", "Manual Approval")], default="draft", required=True)
    default_pricing = fields.Selection([("document", "Document Price"), ("agreed", "Agreed Commercial Price")], default="document", required=True)
    authorized_user_ids = fields.Many2many("res.users", string="Authorized Administrators")
    route_ids = fields.One2many("ff.intercompany.route", "agreement_id")
    company_pair_key = fields.Char(compute="_compute_company_pair_key", store=True, index=True)

    @api.depends("company_a_id", "company_b_id")
    def _compute_company_pair_key(self):
        for record in self:
            ids = sorted((record.company_a_id.id, record.company_b_id.id)) if record.company_a_id and record.company_b_id else []
            record.company_pair_key = ":".join(map(str, ids)) if ids else False

    @api.constrains("company_a_id", "company_b_id", "effective_date", "expiration_date")
    def _check_companies_and_dates(self):
        for record in self:
            if record.company_a_id == record.company_b_id:
                raise ValidationError(_("An intercompany agreement requires two distinct legal companies."))
            if record.expiration_date and record.effective_date and record.expiration_date < record.effective_date:
                raise ValidationError(_("Expiration date must be on or after the effective date."))

    @api.constrains("active", "status", "company_pair_key")
    def _check_active_pair(self):
        for record in self:
            if record.active and record.status == "active" and record.company_pair_key:
                duplicate = self.search_count([
                    ("id", "!=", record.id), ("active", "=", True), ("status", "=", "active"),
                    ("company_pair_key", "=", record.company_pair_key),
                ])
                if duplicate:
                    raise ValidationError(_("Only one active agreement is allowed for a company pair."))


class IntercompanyCounterparty(models.Model):
    _name = "ff.intercompany.counterparty"
    _description = "Intercompany Counterparty Mapping"
    _order = "company_a_id, company_b_id, id"

    name = fields.Char(required=True)
    agreement_id = fields.Many2one("ff.intercompany.agreement", required=True, ondelete="cascade")
    company_a_id = fields.Many2one("res.company", required=True, ondelete="restrict", string="Company A")
    company_b_id = fields.Many2one("res.company", required=True, ondelete="restrict", string="Company B")
    partner_a_id = fields.Many2one("res.partner", required=True, ondelete="restrict", string="Company A Contact")
    partner_b_id = fields.Many2one("res.partner", required=True, ondelete="restrict", string="Company B Contact")
    active = fields.Boolean(default=True)
    role = fields.Selection([("trade", "Trade Counterparty"), ("supplier_customer", "Supplier and Customer")], default="trade", required=True)

    _uniq_mapping = models.Constraint(
        "unique(agreement_id, partner_a_id, partner_b_id)",
        "This contact pair is already mapped for the agreement.",
    )

    @api.constrains("agreement_id", "company_a_id", "company_b_id", "partner_a_id", "partner_b_id")
    def _check_mapping(self):
        for mapping in self:
            if mapping.company_a_id == mapping.company_b_id:
                raise ValidationError(_("Counterparty mapping requires two distinct companies."))
            if mapping.agreement_id and {mapping.company_a_id.id, mapping.company_b_id.id} != {mapping.agreement_id.company_a_id.id, mapping.agreement_id.company_b_id.id}:
                raise ValidationError(_("Counterparty companies must belong to the agreement."))
            if mapping.partner_a_id == mapping.partner_b_id:
                raise ValidationError(_("Company contacts must be distinct records."))
            for partner, company in ((mapping.partner_a_id, mapping.company_a_id), (mapping.partner_b_id, mapping.company_b_id)):
                if partner.company_id and partner.company_id != company:
                    raise ValidationError(_("Contact %s is assigned to another company.") % partner.display_name)


class ResPartner(models.Model):
    _inherit = "res.partner"

    is_intercompany_counterparty = fields.Boolean(
        string="Intercompany Counterparty",
        compute="_compute_intercompany_counterparty",
        search="_search_intercompany_counterparty",
        help="This contact participates in an approved intercompany contact mapping.",
    )
    intercompany_status = fields.Selection(
        [("counterparty", "Intercompany Counterparty")],
        string="Intercompany Status",
        compute="_compute_intercompany_counterparty",
    )

    def _compute_intercompany_counterparty(self):
        mapping_model = self.env["ff.intercompany.counterparty"].sudo()
        partner_ids = set(mapping_model.search([
            "|", ("partner_a_id", "in", self.ids), ("partner_b_id", "in", self.ids),
            ("active", "=", True), ("agreement_id.active", "=", True),
        ]).mapped("partner_a_id").ids)
        partner_ids.update(mapping_model.search([
            ("partner_b_id", "in", self.ids), ("active", "=", True), ("agreement_id.active", "=", True),
        ]).mapped("partner_b_id").ids)
        for partner in self:
            is_counterparty = partner.id in partner_ids
            partner.is_intercompany_counterparty = is_counterparty
            partner.intercompany_status = "counterparty" if is_counterparty else False

    @api.model
    def _search_intercompany_counterparty(self, operator, value):
        mapping_model = self.env["ff.intercompany.counterparty"].sudo()
        ids = mapping_model.search([("active", "=", True), ("agreement_id.active", "=", True)]).mapped("partner_a_id").ids
        ids += mapping_model.search([("active", "=", True), ("agreement_id.active", "=", True)]).mapped("partner_b_id").ids
        return [("id", "in" if (operator == "=" and value) or (operator == "in" and value) else "not in", list(set(ids)))]


class IntercompanyRoute(models.Model):
    _name = "ff.intercompany.route"
    _description = "Intercompany Synchronization Route"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    agreement_id = fields.Many2one("ff.intercompany.agreement", required=True, ondelete="cascade")
    counterparty_mapping_id = fields.Many2one("ff.intercompany.counterparty", ondelete="restrict", string="Counterparty Mapping")
    source_company_id = fields.Many2one("res.company", required=True, ondelete="restrict")
    destination_company_id = fields.Many2one("res.company", required=True, ondelete="restrict")
    source_document = fields.Selection([("sale.order", "Sales Order"), ("purchase.order", "Purchase Order"), ("account.move", "Invoice")], required=True)
    target_document = fields.Selection([("purchase.order", "Purchase Order"), ("sale.order", "Sales Order"), ("account.move", "Vendor Bill")], required=True)
    trigger = fields.Selection([("confirmation", "On Confirmation"), ("posting", "On Posting"), ("manual", "Manual")], required=True, default="confirmation")
    approval_policy = fields.Selection([("draft", "Create Draft"), ("auto", "Auto Confirm"), ("manual", "Manual Approval")], required=True, default="draft")
    pricing_policy = fields.Selection([("document", "Document Price"), ("agreed", "Agreed Commercial Price")], required=True, default="document")
    active = fields.Boolean(default=True)
    source_partner_id = fields.Many2one("res.partner", ondelete="restrict", string="Source Partner")
    destination_partner_id = fields.Many2one("res.partner", ondelete="restrict", string="Destination Partner")
    source_warehouse_id = fields.Many2one("stock.warehouse", ondelete="restrict", string="Source Warehouse")
    destination_warehouse_id = fields.Many2one("stock.warehouse", ondelete="restrict", string="Destination Warehouse")
    integration_user_id = fields.Many2one("res.users", ondelete="restrict")

    @api.constrains("agreement_id", "counterparty_mapping_id", "source_company_id", "destination_company_id", "source_document", "target_document", "active", "source_partner_id", "destination_partner_id")
    def _check_route(self):
        for route in self:
            if route.source_company_id == route.destination_company_id:
                raise ValidationError(_("A route requires two distinct companies."))
            if route.agreement_id and {route.source_company_id.id, route.destination_company_id.id} != {route.agreement_id.company_a_id.id, route.agreement_id.company_b_id.id}:
                raise ValidationError(_("Route companies must belong to its agreement."))
            if route.source_document == "sale.order" and route.target_document != "purchase.order":
                raise ValidationError(_("Sales Order routes must target Purchase Orders in this release."))
            if route.source_document == "purchase.order" and route.target_document != "sale.order":
                raise ValidationError(_("Purchase Order routes must target Sales Orders in this release."))
            if route.active and (route.source_partner_id or route.destination_partner_id) and not route.counterparty_mapping_id:
                raise ValidationError(_("An active route with contacts requires an explicit counterparty mapping."))
            if route.counterparty_mapping_id:
                mapping = route.counterparty_mapping_id
                if mapping.agreement_id != route.agreement_id:
                    raise ValidationError(_("The counterparty mapping must belong to the route agreement."))
                expected = {(mapping.company_a_id.id, mapping.company_b_id.id), (mapping.company_b_id.id, mapping.company_a_id.id)}
                if (route.source_company_id.id, route.destination_company_id.id) not in expected:
                    raise ValidationError(_("The counterparty mapping companies do not match the route."))
                source_partner = mapping.partner_a_id if route.source_company_id == mapping.company_a_id else mapping.partner_b_id
                destination_partner = mapping.partner_b_id if route.destination_company_id == mapping.company_b_id else mapping.partner_a_id
                if route.source_partner_id and route.source_partner_id != source_partner:
                    raise ValidationError(_("Route source contact does not match the counterparty mapping."))
                if route.destination_partner_id and route.destination_partner_id != destination_partner:
                    raise ValidationError(_("Route destination contact does not match the counterparty mapping."))


class IntercompanyEvent(models.Model):
    _name = "ff.intercompany.event"
    _description = "Intercompany Synchronization Event"
    _inherit = ["mail.thread"]
    _order = "create_date desc"

    name = fields.Char(required=True, default=lambda self: self.env["ir.sequence"].next_by_code("ff.intercompany.event") or "/")
    source_company_id = fields.Many2one("res.company", required=True, index=True, ondelete="restrict")
    destination_company_id = fields.Many2one("res.company", required=True, index=True, ondelete="restrict")
    source_model = fields.Char(required=True, index=True)
    source_res_id = fields.Integer(required=True, index=True)
    target_model = fields.Char(required=True)
    target_res_id = fields.Integer(index=True)
    route_id = fields.Many2one("ff.intercompany.route", required=True, ondelete="restrict")
    event_type = fields.Selection([("confirmation", "Confirmation"), ("posting", "Posting"), ("manual", "Manual")], required=True)
    idempotency_key = fields.Char(required=True, index=True)
    state = fields.Selection([("pending", "Pending"), ("processing", "Processing"), ("completed", "Completed"), ("failed", "Failed"), ("blocked", "Blocked"), ("cancelled", "Cancelled")], default="pending", required=True, tracking=True)
    approval_status = fields.Selection([("not_required", "Not Required"), ("pending", "Awaiting Approval"), ("approved", "Approved")], default="not_required", required=True, tracking=True)
    attempt_count = fields.Integer(default=0)
    last_error_code = fields.Char()
    last_error = fields.Text(groups="ff_intercompany_base.group_intercompany_manager")
    last_attempt = fields.Datetime()
    next_retry = fields.Datetime()
    triggered_by_id = fields.Many2one("res.users", default=lambda self: self.env.user, ondelete="restrict")
    processed_at = fields.Datetime()
    origin_event_id = fields.Many2one("ff.intercompany.event", ondelete="restrict")

    _uniq_idempotency_key = models.Constraint(
        "unique(idempotency_key)",
        "An intercompany event already exists for this source operation.",
    )

    @api.model
    def make_key(self, route, model, res_id, event_type):
        raw = json.dumps([route.id, model, res_id, event_type], separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    @api.model
    def create_for_document(self, route, source, event_type):
        key = self.make_key(route, source._name, source.id, event_type)
        existing = self.search([("idempotency_key", "=", key)], limit=1)
        if existing:
            return existing
        return self.create({
            "source_company_id": route.source_company_id.id,
            "destination_company_id": route.destination_company_id.id,
            "source_model": source._name,
            "source_res_id": source.id,
            "target_model": route.target_document,
            "route_id": route.id,
            "event_type": event_type,
            "idempotency_key": key,
            "origin_event_id": self.env.context.get("ff_intercompany_origin_event_id"),
        })

    def action_retry(self):
        if not self.env.user.has_group("ff_intercompany_base.group_intercompany_manager"):
            raise UserError(_("Only intercompany managers can retry events."))
        self.write({"state": "pending", "last_error": False, "last_error_code": False})
        return True


class IntercompanyDocumentLink(models.Model):
    _name = "ff.intercompany.document.link"
    _description = "Intercompany Document Link"
    _order = "create_date desc"

    agreement_id = fields.Many2one("ff.intercompany.agreement", required=True, ondelete="restrict")
    route_id = fields.Many2one("ff.intercompany.route", required=True, ondelete="restrict")
    source_company_id = fields.Many2one("res.company", required=True, ondelete="restrict")
    target_company_id = fields.Many2one("res.company", required=True, ondelete="restrict")
    source_model = fields.Char(required=True, index=True)
    source_res_id = fields.Integer(required=True, index=True)
    target_model = fields.Char(required=True)
    target_res_id = fields.Integer(required=True, index=True)
    operation_type = fields.Selection([("trade", "Trade"), ("accounting", "Accounting")], required=True, default="trade")
    synchronization_status = fields.Selection([("active", "Active"), ("cancelled", "Cancelled"), ("exception", "Exception")], default="active", required=True)
    event_id = fields.Many2one("ff.intercompany.event", required=True, ondelete="restrict")

    _uniq_source_route = models.Constraint(
        "unique(route_id, source_model, source_res_id)",
        "A source document already has a counterpart for this route.",
    )

    @api.model
    def create_for_event(self, event, target):
        existing = self.search([("event_id", "=", event.id)], limit=1)
        if existing:
            return existing
        return self.create({
            "agreement_id": event.route_id.agreement_id.id,
            "route_id": event.route_id.id,
            "source_company_id": event.source_company_id.id,
            "target_company_id": event.destination_company_id.id,
            "source_model": event.source_model,
            "source_res_id": event.source_res_id,
            "target_model": target._name,
            "target_res_id": target.id,
            "event_id": event.id,
        })


class IntercompanyEventService(models.AbstractModel):
    _name = "ff.intercompany.event.service"
    _description = "Intercompany Event Service"

    def process(self, event):
        event.ensure_one()
        if event.state == "completed":
            return self.env[event.target_model].browse(event.target_res_id).exists()
        event.write({"state": "processing", "attempt_count": event.attempt_count + 1, "last_attempt": fields.Datetime.now()})
        try:
            target = self._process_event(event)
            event.write({
                "state": "completed", "target_res_id": target.id, "processed_at": fields.Datetime.now(),
                "approval_status": "pending" if event.route_id.approval_policy == "manual" else "approved",
                "last_error": False, "last_error_code": False,
            })
            self.env["ff.intercompany.document.link"].create_for_event(event, target)
            return target
        except (ValidationError, UserError) as exc:
            event.write({"state": "blocked", "last_error_code": type(exc).__name__, "last_error": str(exc)})
            _logger.warning("Intercompany event %s blocked: %s", event.id, exc)
            return self.env[event.target_model].browse()
        except Exception as exc:  # noqa: BLE001 - worker-safe durable error capture
            event.write({"state": "failed", "last_error_code": type(exc).__name__, "last_error": str(exc)})
            _logger.exception("Intercompany event %s failed", event.id)
            return self.env[event.target_model].browse()

    def _process_event(self, event):
        raise UserError(_("No processor is registered for route %s.") % event.route_id.display_name)
