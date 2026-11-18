from __future__ import annotations

from sqlite3 import Connection

from device.commands import Command, ElasticCommand
from device.elastic import record_elastic_trigger
from device.journal import record_trigger
from sampling.elastic import (
    PendingExposure,
    arrangement_exists,
    load_unsigned_elastic_tickets,
    pending_exposures,
)
from sampling.queue import due_task
from shift.calendar import Calendar


def dispatch_due(db: Connection, shift_id: str, beat: int) -> Command | None:
    task = due_task(db, shift_id, beat)
    if task is None:
        return None
    return record_trigger(db, task.shift_id, task.ticket_id, task.probe, task.beat)


def dispatch_elastic(
    db: Connection, calendar: Calendar, shift_id: str, beat: int
) -> ElasticCommand | None:
    """Fire at this beat only if some permitted probe keeps a plan covering all work."""
    allowed: tuple[str, ...] = ()
    for slot_beat, probes in calendar.slots:
        if slot_beat == beat:
            allowed = probes
            break
    unsigned = load_unsigned_elastic_tickets(db, shift_id)
    pending = pending_exposures(db, shift_id)
    for probe in allowed:
        candidate = (*pending, PendingExposure(probe, beat))
        if arrangement_exists(calendar, beat, unsigned, candidate):
            return record_elastic_trigger(db, shift_id, probe, beat)
    return None
