import logging
from collections.abc import Sequence

from spindle_core.engine import Command, Event, SagaState, decide

from .ports import ConcurrencyConflict, SagaRepository

logger = logging.getLogger(__name__)


class UnknownSaga(Exception):
    """An event arrived for a saga that was never started."""


class Runner:
    def __init__(self, repo: SagaRepository, max_attempts: int = 5) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._repo = repo
        self._max_attempts = max_attempts

    def start(self, state: SagaState, commands: Sequence[Command]) -> None:
        """Persist the output of start_saga(). Version 0 = 'must not exist yet'."""
        self._repo.save(state.saga_id, state, commands, expected_version=0)

    def handle_event(self, saga_id: str, event: Event) -> None:
        """Returns only after the result is durably saved. Caller acks AFTER this returns."""
        for attempt in range(1, self._max_attempts + 1):
            loaded = self._repo.load(saga_id)
            if loaded is None:
                raise UnknownSaga(saga_id)

            new_state, commands = decide(loaded.state, event)

            if not commands and new_state == loaded.state:
                return  # duplicate event: nothing changed, nothing to save

            try:
                self._repo.save(
                    saga_id, new_state, commands, expected_version=loaded.version
                )
                return
            except ConcurrencyConflict:
                logger.info(
                    "conflict on saga %s (attempt %d), retrying", saga_id, attempt
                )

        raise ConcurrencyConflict(
            f"gave up on saga {saga_id} after {self._max_attempts} attempts"
        )