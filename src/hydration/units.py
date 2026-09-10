"""Unit conversion, and the only place in the application it happens.

Everything in the database is metric: millilitres, kilograms, degrees Celsius,
milligrams. Every number a human types or reads is litres, pounds and degrees
Fahrenheit -- except milligrams of sodium and caffeine, which are milligrams
everywhere because that is how supplement labels print them.

Keeping the conversion at the edge is the same discipline the timestamps get:
one storage representation, converted only where a person is involved. A
conversion that leaks inward is how you end up with a column holding a mix of
both and no way to tell which is which.
"""

from __future__ import annotations

ML_PER_L = 1000.0
LB_PER_KG = 2.2046226218487757
ML_PER_FL_OZ = 29.5735295625


# -- volume ----------------------------------------------------------------

def ml_to_l(ml: float | None) -> float | None:
    return None if ml is None else ml / ML_PER_L


def l_to_ml(litres: float | None) -> float | None:
    return None if litres is None else litres * ML_PER_L


def format_l(ml: float | None, places: int = 2) -> str:
    """Render millilitres as litres for display, e.g. 350 -> '0.35 L'."""
    if ml is None:
        return "--"
    return f"{ml / ML_PER_L:.{places}f} L"


# -- mass ------------------------------------------------------------------

def kg_to_lb(kg: float | None) -> float | None:
    return None if kg is None else kg * LB_PER_KG


def lb_to_kg(lb: float | None) -> float | None:
    return None if lb is None else lb / LB_PER_KG


def format_lb(kg: float | None, places: int = 1) -> str:
    if kg is None:
        return "--"
    return f"{kg * LB_PER_KG:.{places}f} lb"


# -- temperature -----------------------------------------------------------

def c_to_f(celsius: float | None) -> float | None:
    return None if celsius is None else celsius * 9.0 / 5.0 + 32.0


def f_to_c(fahrenheit: float | None) -> float | None:
    return None if fahrenheit is None else (fahrenheit - 32.0) * 5.0 / 9.0


def format_f(celsius: float | None, places: int = 0) -> str:
    if celsius is None:
        return "--"
    return f"{c_to_f(celsius):.{places}f}°F"


# -- accepting input -------------------------------------------------------
#
# HASS and the browser both post numbers as strings, and a blank field is a
# real case (an optional void volume) that must not become 0.0.

def parse_optional_float(raw: str | float | None) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = raw.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        from .errors import ValidationError

        raise ValidationError(f"{raw!r} is not a number") from None
