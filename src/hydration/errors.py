"""The three errors the web layer knows how to render as a flash message.

Anything else reaching a request handler is a real bug and should 500 loudly
rather than be swallowed into a friendly-looking page.
"""


class HydrationError(Exception):
    """Base for errors this application raises deliberately."""


class ValidationError(HydrationError):
    """The input could not be accepted -- bad number, unknown beverage, a
    timestamp in the future."""


class ConflictError(HydrationError):
    """The input was well-formed but violates an invariant -- logging a second
    profile, voiding an already-voided row."""


class NotFound(HydrationError):
    """No such row."""
