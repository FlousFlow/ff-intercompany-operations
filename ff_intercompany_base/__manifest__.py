{
    "name": "FF Intercompany Operations - Base",
    "version": "20.0.1.0.0",
    "category": "Operations",
    "summary": "Configuration-driven intercompany synchronization for Odoo 20",
    "description": """
FF Intercompany Operations provides a controlled, configuration-driven foundation for synchronizing documents between legal companies.

Features include company agreements, explicit counterparty mappings, route policies, durable events, idempotency protection, multi-company security, and Arabic translations.
""",
    "author": "Flous Flow",
    "website": "https://flousflow.com",
    "support": "support@flousflow.com",
    "license": "LGPL-3",
    "depends": ["base", "mail", "stock"],
    "data": [
        "security/security.xml",
        "security/ir.access.csv",
        "data.xml",
        "views/intercompany_views.xml",
        "wizard/intercompany_setup_views.xml",
    ],
    "installable": True,
    "application": True,
    "maintainers": ["Flous Flow"],
}
