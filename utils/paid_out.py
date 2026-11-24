"""Live paid-out counter — what the network has actually paid referrers.

The counter reads the Payout ledger only: it sums ``Payout.amount``
where ``status='completed'``, grouped by currency. It never moves
money and never calls Stripe.

Per-currency totals, never a blended sum: CA$, US$ and EUR are three
separate figures on the homepage counter, because there is no FX
conversion anywhere in Paid2Match (see utils/currency.py) — adding
dollars and euros together would be a meaningless number.

Existing Payout rows store lowercase codes ('cad'), so the grouping
uppercases the currency before summing.
"""

from decimal import Decimal

from sqlalchemy import func

from extensions import db
from models.payout import Payout
from utils.currency import CURRENCIES


def paid_out_totals():
    """Per-currency totals of completed payouts.

    Returns ``{currency_code: total}`` with one entry per supported
    currency (``CURRENCIES``), each a Decimal — ``Decimal('0')`` when
    nothing has been paid out in that currency yet, so the homepage
    counter renders CA$0.00 / US$0.00 / €0.00 on a fresh site.
    Pending, processing and failed payouts are excluded: only money
    that actually reached a referrer counts.
    """
    totals = {code: Decimal('0') for code in CURRENCIES}
    rows = (
        db.session.query(
            func.upper(Payout.currency),
            func.sum(Payout.amount),
        )
        .filter(Payout.status == 'completed')
        .group_by(func.upper(Payout.currency))
        .all()
    )
    for code, total in rows:
        # A payout in an unsupported currency (legacy data, or a
        # currency added to payouts before CURRENCIES) is ignored:
        # the counter only ever shows the three supported figures.
        if code in totals and total is not None:
            totals[code] = total
    return totals
