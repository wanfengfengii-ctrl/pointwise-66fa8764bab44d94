from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    trigger_id: int
    shift_id: str
    ticket_id: str
    probe: str
    beat: int


@dataclass(frozen=True)
class ElasticCommand:
    trigger_id: int
    shift_id: str
    probe: str
    beat: int


def device_payload(command: Command | ElasticCommand) -> dict[str, int | str]:
    payload = {
        'trigger_id': command.trigger_id,
        'shift_id': command.shift_id,
        'probe': command.probe,
        'beat': command.beat,
    }
    if isinstance(command, Command):
        payload['ticket_id'] = command.ticket_id
    return payload
