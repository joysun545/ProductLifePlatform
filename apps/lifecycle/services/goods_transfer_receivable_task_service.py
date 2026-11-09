"""Merchant down-shipment documents no longer create monetary tasks.

Keep this public entry point because account workflows and the diagnostic
management command import it. Reading or retrying it never creates a ledger,
payment task, allocation, or database transition. Factory order finance uses
its separate order_receivable_task_service and is unchanged.
"""


def reconcile_receivable_tasks(settlement, actor_openid=""):
    return {
        "closed": 0,
        "created": 0,
        "active_transfer_id": None,
        "financial_enabled": False,
        "reason": "MERCHANT_DOWNSTREAM_DOCUMENTS_ONLY",
    }
