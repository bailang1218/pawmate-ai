"""Desktop-pet state-machine placeholder.

Standing is implemented first. Sitting/work and sleeping loops can be added
here once their frame plans are finalized.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PetState:
    posture: str = "standing"
    activity: str = "idle"

