{
    "name": "Intercompany Operations",
    "version": "20.0.1.0.0",
    "category": "Operations",
    "summary": "Configuration-driven intercompany synchronization for Odoo 20",
    "description": """
FF Intercompany Operations is one installable Odoo application for controlled
Sales/Purchase and Accounting synchronization between legal companies.

It includes company agreements, explicit counterparty mappings, Quick Setup,
Sales Order/Purchase Order synchronization, draft vendor bills and credit
notes, durable events, idempotency protection, multi-company security, and
Arabic translations.
""",
    "author": "Flous Flow",
    "website": "https://flousflow.com",
    "support": "support@flousflow.com",
    "license": "LGPL-3",
    "images": [
        "static/description/cover.png",
        "static/description/quick_setup.png",
        "static/description/setup_help.png",
    ],
    "depends": ["base", "mail", "stock", "sale_management", "purchase", "account"],
    "data": [
        "security/security.xml",
        "security/ir.access.csv",
        "data.xml",
        "views/intercompany_views.xml",
        "wizard/intercompany_setup_views.xml",
        "views/trade_views.xml",
        "views/account_views.xml",
    ],
    "installable": True,
    "application": True,
    "maintainers": ["Flous Flow"],
}
