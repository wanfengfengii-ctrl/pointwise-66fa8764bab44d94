from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection

from device.commands import ElasticCommand
from device.journal import allocate_trigger_id
from shift.storage import decode_integer, encode_integer, validate_record_id


@dataclass(frozen=True)
class ElasticTrigger:
    command: ElasticCommand
    receipt_id: int | None


def create_elastic_trigger_schema(db: Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS elastic_triggers (
            trigger_id INTEGER PRIMARY KEY,
            shift_id TEXT NOT NULL REFERENCES calendars(shift_id),
            probe TEXT NOT NULL,
            beat TEXT NOT NULL,
            receipt_id INTEGER UNIQUE REFERENCES elastic_receipts(receipt_id),
            UNIQUE (shift_id, beat)
        )
    ''')


def record_elastic_trigger(
    db: Connection, shift_id: str, probe: str, beat: int
) -> ElasticCommand:
    trigger_id = allocate_trigger_id(db)
    db.execute(
        'INSERT INTO elastic_triggers (trigger_id, shift_id, probe, beat) VALUES (?, ?, ?, ?)',
        (trigger_id, shift_id, probe, encode_integer(beat)),
    )
    return ElasticCommand(trigger_id, shift_id, probe, beat)


def _restore_elastic_trigger(row: tuple[int, str, str, str, int | None]) -> ElasticTrigger:
    trigger_id, shift_id, probe, beat, receipt_id = row
    command = ElasticCommand(trigger_id, shift_id, probe, decode_integer(beat))
    return ElasticTrigger(command, receipt_id)


def load_elastic_trigger(db: Connection, trigger_id: int) -> ElasticTrigger:
    validate_record_id(trigger_id, 'trigger')
    row = db.execute(
        '''SELECT trigger_id, shift_id, probe, beat, receipt_id
           FROM elastic_triggers WHERE trigger_id = ?''',
        (trigger_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f'unknown trigger {trigger_id}')
    return _restore_elastic_trigger(row)


def list_pending_elastic_triggers(db: Connection, shift_id: str) -> tuple[ElasticTrigger, ...]:
    rows = db.execute(
        '''SELECT trigger_id, shift_id, probe, beat, receipt_id
           FROM elastic_triggers WHERE shift_id = ? AND receipt_id IS NULL
           ORDER BY trigger_id''',
        (shift_id,),
    )
    return tuple(_restore_elastic_trigger(row) for row in rows)


def complete_elastic_trigger(db: Connection, trigger_id: int, receipt_id: int) -> None:
    updated = db.execute(
        '''UPDATE elastic_triggers SET receipt_id = ?
           WHERE trigger_id = ? AND receipt_id IS NULL
             AND EXISTS (SELECT 1 FROM elastic_receipts
                         WHERE receipt_id = ? AND trigger_id = ?)''',
        (receipt_id, trigger_id, receipt_id, trigger_id),
    )
    if updated.rowcount != 1:
        raise ValueError('completion requires a pending trigger and its own receipt')
