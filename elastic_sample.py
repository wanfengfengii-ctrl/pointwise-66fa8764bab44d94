"""Play the hand-computable elastic sample from elastic-example.json.

The player is shared by the demo and the joint verification tests. Source beats in
the events are fixture lookup keys; the device always replies with trigger numbers.
"""
from __future__ import annotations

from sampling.planner import book_elastic
from shift.calibration import Calibration
from shift.register import register_elastic


def run_sample(db, sample, device):
    """Register the sample shift and play its events.

    Returns (commands, receipts): the dispatched elastic commands and the signed
    receipts, both keyed by their source beat.
    """
    shift_id = sample['shift_id']
    calibrations = {
        name: Calibration(correction, tolerance)
        for name, (correction, tolerance) in sample['calibrations'].items()
    }
    register_elastic(db, shift_id, dict(sample['slots']), calibrations)
    commands = {}
    receipts = {}
    for event in sample['events']:
        kind = event[0]
        if kind == 'book':
            _, ticket_id, first, last, probe, target = event
            if book_elastic(db, shift_id, ticket_id, first, last, probe, target) != ticket_id:
                raise AssertionError(f'book {ticket_id} did not return the ticket number')
        elif kind == 'reject_book':
            _, ticket_id, first, last, probe, target = event
            try:
                book_elastic(db, shift_id, ticket_id, first, last, probe, target)
            except ValueError:
                pass
            else:
                raise AssertionError(f'reject_book {ticket_id} was accepted')
        elif kind == 'tick':
            _, beat = event
            command = device.advance(db, shift_id, beat)
            if command is not None:
                commands[beat] = command
        elif kind == 'receive':
            _, beat, raw = event
            receipts[beat] = device.release(db, commands[beat].trigger_id, raw)
        elif kind == 'replay':
            _, beat, raw = event
            if device.release(db, commands[beat].trigger_id, raw) != receipts[beat]:
                raise AssertionError(f'replay at beat {beat} changed the receipt')
        elif kind == 'reject_reply':
            _, beat, raw = event
            try:
                device.release(db, commands[beat].trigger_id, raw)
            except ValueError:
                pass
            else:
                raise AssertionError(f'reject_reply at beat {beat} was signed')
        else:
            raise AssertionError(f'unknown event {event!r}')
    return commands, receipts


def receipt_window_facts(result):
    measured = result.measurement
    return [
        result.beat, result.ticket_id, measured.raw_um, measured.target_um,
        measured.corrected_um, measured.deviation_um, result.verdict.value,
    ]


def check_expected_receipts(sample, commands, receipts):
    """Compare played outcomes with the sample's hand-computable expectations."""
    expected = [list(row) for row in sample['expected_receipts']]
    actual = [receipt_window_facts(receipts[row[0]]) for row in expected]
    if actual != expected:
        raise AssertionError(f'elastic receipts differ: expected {expected!r}, got {actual!r}')
    for row in expected:
        beat = row[0]
        command, result = commands[beat], receipts[beat]
        facts = (command.trigger_id, command.shift_id, command.probe, command.beat)
        if facts != (result.trigger_id, sample['shift_id'], result.probe, beat):
            raise AssertionError(f'command facts differ at beat {beat}: {facts!r}')
    return actual
