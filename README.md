# FF Intercompany Operations

Odoo 20 Community implementation for configuration-driven intercompany operations.

## Apps Store release

This release contains one installable Odoo application: `ff_intercompany_operations`. It includes the base agreement/route engine, Sales/Purchase synchronization, and Accounting document synchronization in one package.

The safest production policy is to create destination documents as drafts. Review the company pair, counterparty contacts, warehouses, journals, currencies, taxes, and access groups before activating a route. Automatic confirmation is intentionally opt-in.

### User workflow

1. Open **Intercompany Operations → Quick Setup** as an Intercompany Manager.
2. Select the source and destination companies and the document direction.
3. Keep destination documents as drafts unless the business process explicitly requires automatic confirmation.
4. Odoo detects the company counterparty contacts automatically. Manual mapping is available only for exceptional cases.
5. Review or deactivate existing agreements from **Intercompany Operations → Company Agreements**.

The contact badge **Intercompany Counterparty** is informational and is calculated from an active counterparty mapping; it is not a manual checkbox.

## Current scope

* Agreements, routes, durable events, document links, Odoo 20 `ir.access` security, and idempotency constraints.
* Explicit counterparty mappings mark the participating contacts and are validated against active routes.
* Native Sales Order → draft Purchase Order and Purchase Order → draft Sales Order synchronization.
* Posted customer invoice / credit note → destination vendor bill / vendor credit note synchronization.
* Counterpart documents are created in the configured destination company and remain draft by default.
* Route policy `Auto Confirm` confirms only the commercial counterpart; it does not validate receipts or post invoices.
* Sales Orders and Purchase Orders expose intercompany status and counterpart references.
* Product taxes are not copied across companies; destination-company native defaults must determine them.
* Accounting counterpart documents remain draft by default; payment, reconciliation, receipt validation, and stock movement are not automated.
* Optional stock fulfillment, partial receipts, backorders, returns, and lot/serial traceability are not included in this release.

## Publishing notes

* This repository contains one installable addon: `ff_intercompany_operations`.
* Arabic translations are shipped in `ff_intercompany_operations/i18n/ar_001.po`.
* The default route policy is Draft. Review company mappings, journals, currencies, and access groups before enabling Auto Confirm or production installation.

## Odoo 20 test command

```bash
/Users/mohamedgamal/Desktop/odoo20/odoo20-venv/bin/python \
  /Users/mohamedgamal/Desktop/odoo20/odoo-bin \
  -d odoo20_module_test --db_host=/tmp --db_user=mohamedgamal \
  --addons-path=/Users/mohamedgamal/Desktop/odoo20/addons,/Users/mohamedgamal/Desktop/odoo20/odoo/addons,$PWD \
  --test-enable --test-tags=ff_intercompany_operations \
  --stop-after-init --log-level=test -i ff_intercompany_operations
```

The test database is disposable development data. Do not run installation or upgrades against production without a backup and explicit authorization.
