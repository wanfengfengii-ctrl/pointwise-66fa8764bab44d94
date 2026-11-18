from dataclasses import replace
from sqlite3 import Connection

from device.journal import read_cursor
from sampling.bookings import reserve_beat
from sampling.elastic import (
    arrangement_exists,
    build_elastic_ticket,
    elastic_ticket_exists,
    insert_elastic_ticket,
    load_unsigned_elastic_tickets,
    pending_exposures,
    ticket_number_taken,
)
from sampling.tickets import build_ticket
from shift.calendar import load_calendar
from shift.modes import is_elastic_shift
from shift.storage import write_transaction


def book(
    db: Connection, shift_id: str, ticket_id: str, beat: int, probe: str, target_um: int
) -> str:
    ticket = build_ticket(shift_id, ticket_id, beat, probe, target_um)
    with write_transaction(db):
        calendar = load_calendar(db, ticket.shift_id)
        if is_elastic_shift(db, ticket.shift_id):
            raise ValueError(f'shift {ticket.shift_id!r} is registered for elastic sampling')
        if ticket.beat <= read_cursor(db, ticket.shift_id):
            raise ValueError('the requested beat has already passed')
        if not calendar.allows_probe(ticket.beat, ticket.probe):
            raise ValueError('the probe is not permitted at the requested beat')
        if elastic_ticket_exists(db, ticket.ticket_id):
            raise ValueError('ticket number or shift beat is already reserved')
        reserve_beat(db, ticket)
    return ticket.ticket_id


def book_elastic(
    db: Connection,
    shift_id: str,
    ticket_id: str,
    first_beat: int,
    last_beat: int,
    probe: str,
    target_um: int,
) -> str:
    draft = build_elastic_ticket(shift_id, ticket_id, first_beat, last_beat, probe, target_um)
    with write_transaction(db):
        calendar = load_calendar(db, draft.shift_id)
        if not is_elastic_shift(db, draft.shift_id):
            raise ValueError(f'shift {draft.shift_id!r} is registered for fixed-beat sampling')
        if all(name != draft.probe for name, _ in calendar.calibrations):
            raise ValueError(f'probe {draft.probe!r} is not calibrated for this shift')
        if ticket_number_taken(db, draft.ticket_id):
            raise ValueError('ticket number is already reserved')
        ticket = replace(draft, accepted_cursor=read_cursor(db, draft.shift_id))
        unsigned = (*load_unsigned_elastic_tickets(db, draft.shift_id), ticket)
        pending = pending_exposures(db, draft.shift_id)
        if not arrangement_exists(calendar, ticket.accepted_cursor, unsigned, pending):
            raise ValueError('no conflict-free arrangement can fulfill the ticket')
        insert_elastic_ticket(db, ticket)
    return ticket.ticket_id
