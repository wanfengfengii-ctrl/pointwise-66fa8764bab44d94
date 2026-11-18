from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection

from device.elastic import list_pending_elastic_triggers
from shift.calendar import Calendar
from shift.storage import decode_integer, encode_integer


@dataclass(frozen=True)
class ElasticTicket:
    shift_id: str
    ticket_id: str
    first_beat: int
    last_beat: int
    probe: str
    target_um: int
    accepted_cursor: int
    receipt_id: int | None


@dataclass(frozen=True)
class PendingExposure:
    probe: str
    beat: int


def create_elastic_ticket_schema(db: Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS elastic_tickets (
            ticket_id TEXT PRIMARY KEY NOT NULL,
            shift_id TEXT NOT NULL REFERENCES calendars(shift_id),
            first_beat TEXT NOT NULL,
            last_beat TEXT NOT NULL,
            probe TEXT NOT NULL,
            target TEXT NOT NULL,
            accepted_cursor TEXT NOT NULL,
            receipt_id INTEGER UNIQUE REFERENCES elastic_receipts(receipt_id)
        )
    ''')


def build_elastic_ticket(
    shift_id: str,
    ticket_id: str,
    first_beat: int,
    last_beat: int,
    probe: str,
    target_um: int,
) -> ElasticTicket:
    for label, name in (('shift', shift_id), ('ticket', ticket_id), ('probe', probe)):
        if not isinstance(name, str) or not name:
            raise ValueError(f'{label} must be a nonempty string')
    for label, beat in (('first beat', first_beat), ('last beat', last_beat)):
        if type(beat) is not int or beat <= 0:
            raise ValueError(f'{label} must be a positive integer')
    if first_beat > last_beat:
        raise ValueError('the beat window must not be empty')
    if type(target_um) is not int:
        raise ValueError('target must be integer micrometers')
    return ElasticTicket(shift_id, ticket_id, first_beat, last_beat, probe, target_um, 0, None)


def insert_elastic_ticket(db: Connection, ticket: ElasticTicket) -> None:
    db.execute(
        '''INSERT INTO elastic_tickets
               (ticket_id, shift_id, first_beat, last_beat, probe, target, accepted_cursor)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (
            ticket.ticket_id,
            ticket.shift_id,
            encode_integer(ticket.first_beat),
            encode_integer(ticket.last_beat),
            ticket.probe,
            encode_integer(ticket.target_um),
            encode_integer(ticket.accepted_cursor),
        ),
    )


_ELASTIC_TICKET_COLUMNS = (
    'ticket_id, shift_id, first_beat, last_beat, probe, target, accepted_cursor, receipt_id'
)


def _restore_elastic_ticket(
    row: tuple[str, str, str, str, str, str, str, int | None]
) -> ElasticTicket:
    ticket_id, shift_id, first, last, probe, target, cursor, receipt_id = row
    return ElasticTicket(
        shift_id, ticket_id, decode_integer(first), decode_integer(last), probe,
        decode_integer(target), decode_integer(cursor), receipt_id,
    )


def load_unsigned_elastic_tickets(db: Connection, shift_id: str) -> tuple[ElasticTicket, ...]:
    rows = db.execute(
        f'''SELECT {_ELASTIC_TICKET_COLUMNS} FROM elastic_tickets
            WHERE shift_id = ? AND receipt_id IS NULL ORDER BY rowid''',
        (shift_id,),
    )
    return tuple(_restore_elastic_ticket(row) for row in rows)


def elastic_ticket_exists(db: Connection, ticket_id: str) -> bool:
    row = db.execute(
        'SELECT 1 FROM elastic_tickets WHERE ticket_id = ?', (ticket_id,)
    ).fetchone()
    return row is not None


def ticket_number_taken(db: Connection, ticket_id: str) -> bool:
    """Ticket numbers are unique across both sampling modes in one workspace."""
    row = db.execute('SELECT 1 FROM tickets WHERE ticket_id = ?', (ticket_id,)).fetchone()
    return row is not None or elastic_ticket_exists(db, ticket_id)


def mark_elastic_ticket_signed(db: Connection, ticket_id: str, receipt_id: int) -> None:
    updated = db.execute(
        'UPDATE elastic_tickets SET receipt_id = ? WHERE ticket_id = ? AND receipt_id IS NULL',
        (receipt_id, ticket_id),
    )
    if updated.rowcount != 1:
        raise ValueError('signing requires an unsigned elastic ticket')


def pending_exposures(db: Connection, shift_id: str) -> tuple[PendingExposure, ...]:
    return tuple(
        PendingExposure(item.command.probe, item.command.beat)
        for item in list_pending_elastic_triggers(db, shift_id)
    )


def ticket_may_use_exposure(ticket: ElasticTicket, exposure: PendingExposure) -> bool:
    """A pending exposure only serves tickets accepted before it was triggered."""
    return (
        ticket.probe == exposure.probe
        and ticket.first_beat <= exposure.beat <= ticket.last_beat
        and ticket.accepted_cursor < exposure.beat
    )


def arrangement_exists(
    calendar: Calendar,
    cursor: int,
    tickets: tuple[ElasticTicket, ...],
    pending: tuple[PendingExposure, ...],
) -> bool:
    """Whether every unsigned ticket and every pending exposure still fits one plan.

    Opportunities are the pending exposures plus each future registered beat; one beat
    offers a single opportunity even when it permits several probes.
    """
    future_beats = tuple(beat for beat, _ in calendar.slots if beat > cursor)
    exposures = tuple(pending)
    opportunities = exposures + future_beats

    def eligible(ticket: ElasticTicket, opportunity: int) -> bool:
        if opportunity < len(exposures):
            return ticket_may_use_exposure(ticket, exposures[opportunity])
        beat = opportunities[opportunity]
        return (
            ticket.first_beat <= beat <= ticket.last_beat
            and calendar.allows_probe(beat, ticket.probe)
        )

    ticket_opps = [
        tuple(opp for opp in range(len(opportunities)) if eligible(ticket, opp))
        for ticket in tickets
    ]
    opp_tickets = [
        tuple(index for index, ticket in enumerate(tickets) if eligible(ticket, opp))
        for opp in range(len(opportunities))
    ]
    match_for_ticket = [-1] * len(tickets)
    match_for_opp = [-1] * len(opportunities)

    def augment_exposure(opp: int, seen_tickets: set) -> bool:
        for index in opp_tickets[opp]:
            if index in seen_tickets:
                continue
            seen_tickets.add(index)
            partner = match_for_ticket[index]
            if partner == -1 or augment_exposure(partner, seen_tickets):
                match_for_ticket[index] = opp
                match_for_opp[opp] = index
                return True
        return False

    def augment_ticket(index: int, seen_opps: set) -> bool:
        for opp in ticket_opps[index]:
            if opp in seen_opps:
                continue
            seen_opps.add(opp)
            partner = match_for_opp[opp]
            if partner == -1 or augment_ticket(partner, seen_opps):
                match_for_ticket[index] = opp
                match_for_opp[opp] = index
                return True
        return False

    # Every pending exposure must claim a distinct ticket.
    changed = True
    while changed:
        changed = False
        for opp in range(len(exposures)):
            if match_for_opp[opp] == -1 and augment_exposure(opp, set()):
                changed = True
    if any(match_for_opp[opp] == -1 for opp in range(len(exposures))):
        return False
    # Every unsigned ticket must claim a distinct remaining opportunity.
    changed = True
    while changed:
        changed = False
        for index in range(len(tickets)):
            if match_for_ticket[index] == -1 and augment_ticket(index, set()):
                changed = True
    return all(match != -1 for match in match_for_ticket)
