"""{{ amount|money }} -> 1,234.50 (amounts are shown after the currency symbol)."""
from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def money(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value
    if not amount.is_finite():
        return value
    return f"{amount:,.2f}"
