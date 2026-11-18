from dataclasses import dataclass
from sqlite3 import Connection

from device.commands import Command
from inspection.judgment import Verdict
from inspection.measure import Measurement
from shift.storage import decode_integer, encode_integer, validate_record_id


@dataclass(frozen=True)
class Receipt:
    receipt_id: int
    trigger_id: int
    shift_id: str
    ticket_id: str
    probe: str
    beat: int
    measurement: Measurement
    tolerance_um: int
    verdict: Verdict


def create_receipt_schema(db: Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS receipts (
            receipt_id INTEGER PRIMARY KEY,
            trigger_id INTEGER NOT NULL UNIQUE REFERENCES triggers(trigger_id),
            shift_id TEXT NOT NULL REFERENCES calendars(shift_id),
            ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
            probe TEXT NOT NULL,
            beat TEXT NOT NULL,
            raw TEXT NOT NULL,
            correction TEXT NOT NULL,
            target TEXT NOT NULL,
            corrected TEXT NOT NULL,
            deviation TEXT NOT NULL,
            tolerance TEXT NOT NULL,
            verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'outside')),
            FOREIGN KEY (shift_id, beat, probe)
                REFERENCES calendar_slot_probes(shift_id, beat, probe)
        )
    ''')


def allocate_receipt_id(db: Connection) -> int:
    """Take the next receipt number from the space shared by both shift modes."""
    row = db.execute('''
        SELECT COALESCE(MAX(receipt_id), 0) + 1 AS next_id FROM (
            SELECT receipt_id FROM receipts
            UNION ALL
            SELECT receipt_id FROM elastic_receipts
        )
    ''').fetchone()
    return row[0]


def insert_receipt(
    db: Connection,
    command: Command,
    measurement: Measurement,
    tolerance_um: int,
    verdict: Verdict,
) -> Receipt:
    receipt_id = allocate_receipt_id(db)
    db.execute(
        '''INSERT INTO receipts (receipt_id, trigger_id, shift_id, ticket_id, probe, beat, raw,
                                 correction, target, corrected, deviation, tolerance, verdict)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            receipt_id,
            command.trigger_id,
            command.shift_id,
            command.ticket_id,
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
        receipt_id, command.trigger_id, command.shift_id, command.ticket_id,
        command.probe, command.beat, measurement, tolerance_um, verdict,
    )


_RECEIPT_COLUMNS = (
    'trigger_id, shift_id, ticket_id, probe, beat,'
    ' raw, correction, target, corrected, deviation, tolerance, verdict'
)


def _restore_receipt(receipt_id: int, row: tuple) -> Receipt:
    (trigger_id, shift_id, ticket_id, probe, beat,
     raw, correction, target, corrected, deviation, tolerance, verdict) = row
    measurement = Measurement(
        decode_integer(raw), decode_integer(correction), decode_integer(target),
        decode_integer(corrected), decode_integer(deviation),
    )
    return Receipt(
        receipt_id, trigger_id, shift_id, ticket_id, probe, decode_integer(beat),
        measurement, decode_integer(tolerance), Verdict(verdict),
    )


def receipt(db: Connection, receipt_id: int) -> Receipt:
    validate_record_id(receipt_id, 'receipt')
    row = db.execute(
        f'SELECT {_RECEIPT_COLUMNS} FROM receipts WHERE receipt_id = ?',
        (receipt_id,),
    ).fetchone()
    if row is None:
        row = db.execute(
            f'SELECT {_RECEIPT_COLUMNS} FROM elastic_receipts WHERE receipt_id = ?',
            (receipt_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f'unknown receipt {receipt_id}')
    return _restore_receipt(receipt_id, row)
