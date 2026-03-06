"""
Shared numeric conversion utilities used across the project.
Provides safe conversion functions for Decimal, int, and float values.
"""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation


def safe_decimal(value, default=0):
    """
    Safely convert a value to Decimal with exactly 2 decimal places.
    Handles None, empty strings, and various numeric types.
    """
    if value is None or value == '':
        return Decimal(str(default)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    try:
        decimal_value = Decimal(str(float(value)))
        return decimal_value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (ValueError, TypeError, ArithmeticError):
        return Decimal(str(default)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def to_decimal(val, default="0"):
    """
    Convert value to Decimal with proper quantization (2 decimal places).
    More strict than safe_decimal - doesn't go through float.
    """
    if val is None or val == "":
        return Decimal(default).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    try:
        decimal_val = Decimal(str(val))
        return decimal_val.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return Decimal(default).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def to_int(val, default=0):
    """Convert value to int safely."""
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def safe_float_convert(value, default=0.0):
    """Safely convert value to float."""
    try:
        if value is None or value == '':
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


# Alias for backward-compat call-sites using `safe_int_convert`
safe_int_convert = to_int
