"""Multi-currency support (foundation).

Paid2Match records amounts in one of three currencies — CAD, EUR, USD —
with CAD as the default. There is NO foreign-exchange conversion
anywhere: an amount is recorded and shown exactly as entered, in the
currency the poster chose for the need.

Display uses an unambiguous symbol per currency (``CA$`` / ``€`` /
``US$``) so a bare ``$`` never leaves the reader guessing which dollar
a reward is in.

Existing Payout rows store lowercase codes (``'cad'``), so both
helpers normalise with ``upper()`` before anything else.
"""

CURRENCIES = ['CAD', 'EUR', 'USD']
DEFAULT_CURRENCY = 'CAD'
CURRENCY_SYMBOLS = {'CAD': 'CA$', 'EUR': '€', 'USD': 'US$'}


def validate_currency(code):
    """Return the uppercased currency code, or raise ValueError for
    anything that is not CAD, EUR or USD (including None)."""
    if code is None:
        raise ValueError(
            f"currency must be one of {CURRENCIES}, got None"
        )
    code = str(code).upper()
    if code not in CURRENCIES:
        raise ValueError(
            f"currency must be one of {CURRENCIES}, got {code!r}"
        )
    return code


def format_money(amount, currency=None):
    """Format an amount as entered, e.g. ``CA$1,000.00``.

    Two decimals, thousands separators, and the currency's symbol.
    The code is case-insensitive (``'cad'`` formats as ``CA$``) and
    defaults to CAD when omitted. A ``None`` amount renders as an
    empty string so templates can pipe optional amounts straight
    through the filter; an unknown currency raises ValueError.
    """
    if amount is None:
        return ''
    symbol = CURRENCY_SYMBOLS[validate_currency(
        currency if currency is not None else DEFAULT_CURRENCY
    )]
    return f"{symbol}{float(amount):,.2f}"
