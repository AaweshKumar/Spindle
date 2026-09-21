# Component: Spindle Architecture
# File: publisher.py
# Description: Source code module for the Spindle workflow orchestration platform.

import logging
import threading

from .ports import CommandBus, OutboxStore

logger = logging.getLogger(__name__)


class OutboxRelay:
    def __init__(
        self, outbox: OutboxStore, bus: CommandBus, batch_size: int = 100
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._outbox = outbox
        self._bus = bus
        self._batch_size = batch_size

    def relay_once(self) -> int:
        """Send one batch, oldest first. Returns how many were sent."""
        records = self._outbox.fetch_unsent(self._batch_size)
        for record in records:
            self._bus.publish(record.command)  # (1) hand to the post office
            self._outbox.mark_sent(record.id)  # (2) cross it off the list
        return len(records)

    def run_forever(self, stop: threading.Event, poll_interval: float = 0.5) -> None:
        while not stop.is_set():
            try:
                sent = self.relay_once()
            except Exception:
                logger.exception("relay failed; will retry next cycle")
                sent = 0
            if sent == 0:
                stop.wait(poll_interval)  # idle or failing: pause instead of spinning