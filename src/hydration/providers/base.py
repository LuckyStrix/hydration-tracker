"""What a provider has to produce.

Narrow on purpose. Everything downstream -- the ledger, the calibration, the
activities page -- is written against these two records, so adding a second
provider means writing one adapter and changing nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Activity:
    external_id: str
    started_at: datetime
    duration_s: float
    name: str | None = None
    activity_type: str | None = None
    distance_m: float | None = None
    kcal: float | None = None
    avg_hr: float | None = None

    sweat_ml: float | None = None
    """The provider's own sweat estimate, when it has one. Garmin reports this
    for many activities; it is preferred over ours but loses to a weight
    pair."""

    fluid_consumed_ml: float = 0.0
    temp_c: float | None = None
    humidity_pct: float | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WeighIn:
    at: datetime
    mass_kg: float
    source: str = "garmin"
