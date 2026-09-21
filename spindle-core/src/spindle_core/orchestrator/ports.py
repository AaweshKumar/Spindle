from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from spindle_core.engine import Command, SagaState


class ConcurrencyConflict(Exception):
    """Someone else saved this saga after we loaded it."""


@dataclass(frozen=True)
class LoadedSaga:
    state: SagaState
    version: int  # 0 = doesn't exist yet; every successful save adds 1


@dataclass(frozen=True)
class OutboxRecord:
    id: int  # ever-increasing; defines send order
    command: Command


class SagaRepository(Protocol):
    def load(self, saga_id: str) -> LoadedSaga | None: ...

    def save(
        self,
        saga_id: str,
        new_state: SagaState,
        commands: Sequence[Command],
        expected_version: int,
    ) -> None:
        """Atomically write the state AND queue the commands, or neither.

        Raises ConcurrencyConflict if the stored version != expected_version.
        """
        ...


class OutboxStore(Protocol):
    def fetch_unsent(self, limit: int) -> list[OutboxRecord]: ...
    def mark_sent(self, record_id: int) -> None: ...


class CommandBus(Protocol):
    def publish(self, command: Command) -> None: ...