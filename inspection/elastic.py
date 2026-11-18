from __future__ import annotations

from sqlite3 import Connection

from device.elastic import (
    ElasticTrigger,
    complete_elastic_trigger,
    list_pending_elastic_triggers,
)
from device.journal import read_cursor
from inspection.judgment import Verdict, judge
from inspection.measure import Measurement, measure
from inspection.receipts import Receipt, allocate_receipt_id, receipt
from sampling.elastic import (
    ElasticTicket,
    PendingExposure,
    arrangement_exists,
    load_unsigned_elastic_tickets,
    mark_elastic_ticket_signed,
    ticket_may_use_exposure,
)
from shift.calendar import load_calendar
from shift.storage import encode_integer


def create_elastic_receipt_schema(db: Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS elastic_receipts (
            receipt_id INTEGER PRIMARY KEY,
            trigger_id INTEGER NOT NULL UNIQUE REFERENCES elastic_triggers(trigger_id),
            shift_id TEXT NOT NULL REFERENCES calendars(shift_id),
            ticket_id TEXT NOT NULL REFERENCES elastic_tickets(ticket_id),
            probe TEXT NOT NULL,
            beat TEXT NOT NULL,
            raw TEXT NOT NULL,
            correction TEXT NOT NULL,
            target TEXT NOT NULL,
            corrected TEXT NOT NULL,
            deviation TEXT NOT NULL,
            tolerance TEXT NOT NULL,
            verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'outside'))
        )
    ''')


def insert_elastic_receipt(
    db: Connection,
    trigger: ElasticTrigger,
    ticket: ElasticTicket,
    measurement: Measurement,
    tolerance_um: int,
    verdict: Verdict,
) -> Receipt:
    receipt_id = allocate_receipt_id(db)
    command = trigger.command
    db.execute(
        '''INSERT INTO elastic_receipts
               (receipt_id, trigger_id, shift_id, ticket_id, probe, beat,
                raw, correction, target, corrected, deviation, tolerance, verdict)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            receipt_id,
            command.trigger_id,
            command.shift_id,
            ticket.ticket_id,
            command.probe,
            encode_integer(command.beat),
            encode_integer(measurement.raw_um),
            encode_integer(measurement.correction_um),
            encode_integer(measurement.target_um),
            encode_integer(measurement.corrected_um),
            encode_integer(measurement.deviation_um),
            encode_integer(tolerance_um),
            verdict.value,
        ),
    )
    return Receipt(
        receipt_id, command.trigger_id, command.shift_id, ticket.ticket_id,
        command.probe, command.beat, measurement, tolerance_um, verdict,
    )


def _choose_ticket(
    db: Connection,
    trigger: ElasticTrigger,
    unsigned: tuple[ElasticTicket, ...],
    calendar,
    cursor: int,
) -> ElasticTicket:
    command = trigger.command
    exposure = PendingExposure(command.probe, command.beat)
    remaining_pending = tuple(
        PendingExposure(item.command.probe, item.command.beat)
        for item in list_pending_elastic_triggers(db, command.shift_id)
        if item.command.trigger_id != command.trigger_id
    )
    for ticket in unsigned:
        if not ticket_may_use_exposure(ticket, exposure):
            continue
        rest = tuple(item for item in unsigned if item.ticket_id != ticket.ticket_id)
        if arrangement_exists(calendar, cursor, rest, remaining_pending):
            return ticket
    raise ValueError('no eligible ticket keeps the remaining work arrangeable')


def sign_elastic(db: Connection, trigger: ElasticTrigger, raw_um: int) -> Receipt:
    """Sign a first reading to one eligible ticket, or replay the original receipt."""
    if trigger.receipt_id is not None:
        result = receipt(db, trigger.receipt_id)
        if raw_um != result.measurement.raw_um:
            raise ValueError('reading conflicts with the signed receipt')
        return result
    command = trigger.command
    calendar = load_calendar(db, command.shift_id)
    cursor = read_cursor(db, command.shift_id)
    unsigned = load_unsigned_elastic_tickets(db, command.shift_id)
    ticket = _choose_ticket(db, trigger, unsigned, calendar, cursor)
    calibration = calendar.calibration_for(command.probe)
    measurement = measure(raw_um, calibration.correction_um, ticket.target_um)
    verdict = judge(measurement.deviation_um, calibration.tolerance_um)
    result = insert_elastic_receipt(
        db, trigger, ticket, measurement, calibration.tolerance_um, verdict
    )
    complete_elastic_trigger(db, command.trigger_id, result.receipt_id)
    mark_elastic_ticket_signed(db, ticket.ticket_id, result.receipt_id)
    return result
