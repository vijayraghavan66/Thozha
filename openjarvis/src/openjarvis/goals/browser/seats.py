"""Pure, offline seat selection from an already authorized seat-map snapshot.

This module never opens a provider page, holds seats, or sends a booking action.
It is useful for deterministic fixture tests and for ranking a read-only seat
map returned by an officially authorized provider adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class SeatMapEntry:
    row: str
    number: int
    seat_id: str
    status: str
    section: str
    price_minor: int

    def __post_init__(self) -> None:
        if not self.row or len(self.row) > 12:
            raise ValueError("seat row is invalid")
        if not 1 <= self.number <= 500:
            raise ValueError("seat number is out of bounds")
        if not self.seat_id or len(self.seat_id) > 80:
            raise ValueError("seat id is invalid")
        if self.status not in {"available", "unavailable", "blocked", "unknown"}:
            raise ValueError("seat status is invalid")
        if not self.section or len(self.section) > 80:
            raise ValueError("seat section is invalid")
        if not 0 <= self.price_minor <= 100_000_000:
            raise ValueError("seat price is out of bounds")


@dataclass(frozen=True, slots=True)
class AdjacentSeatSelection:
    row: str
    section: str
    seats: tuple[SeatMapEntry, ...]
    subtotal_minor: int
    fees_minor: int
    total_minor: int
    source_observed_at: str

    @property
    def seat_ids(self) -> tuple[str, ...]:
        return tuple(seat.seat_id for seat in self.seats)


def select_adjacent_seats(
    seat_map: Iterable[SeatMapEntry],
    *,
    count: int,
    max_total_minor: int,
    fees_minor: int = 0,
    allowed_sections: frozenset[str] | None = None,
    preferred_rows: tuple[str, ...] = (),
    source_observed_at: str,
) -> AdjacentSeatSelection | None:
    """Rank adjacent available seats locally; never mutate a provider seat map.

    Adjacency means consecutive seat numbers in one row and section. Unknown,
    blocked, or unavailable seats break a run. Fees are included in the budget.
    Ties prefer the caller's row order, then the center-most seat pair, then
    lower price and stable row/number ordering.
    """
    if not 1 <= count <= 12:
        raise ValueError("seat count is out of bounds")
    if not 0 <= max_total_minor <= 1_000_000_000 or not 0 <= fees_minor <= 100_000_000:
        raise ValueError("seat budget or fees are out of bounds")
    if not source_observed_at or len(source_observed_at) > 80:
        raise ValueError("seat map observation time is required")
    sections = allowed_sections
    seats = tuple(seat_map)
    if any(not isinstance(seat, SeatMapEntry) for seat in seats):
        raise TypeError("seat map entries must be validated SeatMapEntry values")

    candidates: list[tuple[tuple[object, ...], tuple[SeatMapEntry, ...]]] = []
    groups: dict[tuple[str, str], list[SeatMapEntry]] = {}
    for seat in seats:
        if seat.status != "available" or (sections is not None and seat.section not in sections):
            continue
        groups.setdefault((seat.row, seat.section), []).append(seat)

    preferred_order = {row: index for index, row in enumerate(preferred_rows)}
    for (row, section), entries in groups.items():
        entries.sort(key=lambda seat: seat.number)
        for start in range(0, len(entries) - count + 1):
            selection = tuple(entries[start : start + count])
            if any(
                selection[index + 1].number != selection[index].number + 1
                for index in range(len(selection) - 1)
            ):
                continue
            subtotal = sum(seat.price_minor for seat in selection)
            total = subtotal + fees_minor
            if total > max_total_minor:
                continue
            middle = (selection[0].number + selection[-1].number) / 2
            # The row's approximate center is inferred only from this map's
            # numbers, so the selector remains deterministic and data-local.
            row_numbers = sorted(seat.number for seat in entries)
            center = (row_numbers[0] + row_numbers[-1]) / 2
            preferred_rank = preferred_order.get(row, len(preferred_order))
            rank = (preferred_rank, abs(middle - center), total, row, selection[0].number)
            candidates.append((rank, selection))

    if not candidates:
        return None
    _, chosen = min(candidates, key=lambda item: item[0])
    subtotal = sum(seat.price_minor for seat in chosen)
    return AdjacentSeatSelection(
        row=chosen[0].row,
        section=chosen[0].section,
        seats=chosen,
        subtotal_minor=subtotal,
        fees_minor=fees_minor,
        total_minor=subtotal + fees_minor,
        source_observed_at=source_observed_at,
    )
