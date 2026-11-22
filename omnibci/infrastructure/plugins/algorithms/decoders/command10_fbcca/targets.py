"""Fixed Command10 protocol; IDs are independent of visual traversal order."""
from math import pi
from typing import NamedTuple


REFRESH_RATE_HZ = 60
CUE_FRAMES = 60
STIM_FRAMES = 240
RESULT_FRAMES = 90
FREE_CHOICE_EVENT_ID = 11


class Target(NamedTuple):
    target_id: int
    label: str
    row: int
    column: int
    frequency_hz: float
    phase_rad: float


TARGETS = (
    Target(1, 'Up', 1, 3, 8.0, 0),
    Target(2, 'Down', 3, 3, 8.8, pi / 2),
    Target(3, 'Left', 2, 2, 9.6, pi),
    Target(4, 'Right', 2, 4, 10.4, 3 * pi / 2),
    Target(5, 'A', 1, 1, 11.2, 0),
    Target(6, 'X', 1, 5, 12.0, pi / 2),
    Target(7, 'B', 2, 1, 12.8, pi),
    Target(8, 'Y', 2, 5, 13.6, 3 * pi / 2),
    Target(9, 'C', 3, 1, 14.4, 0),
    Target(10, 'Z', 3, 5, 15.2, pi / 2),
)
