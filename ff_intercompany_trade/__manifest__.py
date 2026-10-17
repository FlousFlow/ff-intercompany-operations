{
    "name": "FF Intercompany Operations - Trade",
    "version": "20.0.1.0.0",
    "category": "Operations",
    "summary": "Synchronize Sales Orders and Purchase Orders between companies",
    "description": """
Synchronizes configured Sales Orders and Purchase Orders across legal companies.

Supports draft creation, manual approval, automatic confirmation, source-company pricing, reverse routes, counterparty mapping, warehouse defaults, and durable event tracking.
""",
    "author": "Flous Flow",
    "website": "https://flousflow.com",
    "support": "support@flousflow.com",
    "license": "LGPL-3",
    "depends": ["ff_intercompany_base", "sale_management", "purchase", "stock"],
    "data": ["views/trade_views.xml"],
    "installable": True,
    "maintainers": ["Flous Flow"],
}
