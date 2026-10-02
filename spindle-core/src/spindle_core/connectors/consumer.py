# Component: Spindle Architecture
# File: consumer.py
# Description: ConnectorConsumer — the push-model generic config-driven consumer.
#
#   Reads DispatchStep / DispatchCompensation messages from the work stream
#   (spindle:work:{step_type}), calls the microservice endpoint described by
#   the matching Tapestry config, and publishes the outcome event to
#   spindle:results.
#
#   Layering:
#     Imports downward into streams/ (L1) and workers/ (L1), and sideways into
#     tapestry/ (L2a, same layer).  Never imported by any layer below.

from __future__ import annotations

import logging

import redis

from spindle_core.streams.consumer import consume_loop, ensure_group
from spindle_core.tapestry.loader import TapestryStore

from .caller import HttpClient, UrllibHttpClient, call_endpoint, is_success
from .outcomes import OutcomeTracker

logger = logging.getLogger(__name__)

# ---- Stream key constants -----------------------------------------------

# Per-step-type work streams.  The orchestrator publishes DispatchStep /
# DispatchCompensation commands here (via router.py, future task).
WORK_STREAM_PREFIX = "spindle:work:"         # appended with step_type at runtime
WORK_GROUP_NAME = "spindle-connectors"

# All connector outcome events are published here so the orchestrator's
# consumer can pick them up and drive the saga forward.
RESULTS_STREAM_KEY = "spindle:results"

# -------------------------------------------------------------------------


class ConnectorConsumer:
    """
    Generic config-driven consumer for a single step type.

    For each incoming DispatchStep or DispatchCompensation message:
      1. Load the Tapestry config for the step type.
      2. Build and send the HTTP request.
      3. Evaluate the response with the configured SuccessCheck.
      4. Publish the outcome event (StepSucceeded / StepFailed /
         CompensationSucceeded / CompensationFailed) to spindle:results.
      5. Record the call outcome in the OutcomeTracker.

    The stream key consumed is spindle:work:{step_type}.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        tapestry: TapestryStore,
        step_type: str,
        consumer_name: str,
        tracker: OutcomeTracker | None = None,
        http_client: HttpClient | None = None,
    ) -> None:
        self._r = redis_client
        self._tapestry = tapestry
        self._step_type = step_type
        self._consumer_name = consumer_name
        self._tracker = tracker if tracker is not None else OutcomeTracker()
        self._http = http_client if http_client is not None else UrllibHttpClient()
        self._stream = f"{WORK_STREAM_PREFIX}{step_type}"

    # -- public API --

    def run(self) -> None:
        """Block and process messages until the process is killed."""
        logger.info(
            "connector starting: step_type=%r stream=%r consumer=%r",
            self._step_type,
            self._stream,
            self._consumer_name,
        )
        consume_loop(
            self._r,
            self._consumer_name,
            self._handle_message,
            stream=self._stream,
            group=WORK_GROUP_NAME,
        )

    def handle_once(self, message_id: str, fields: dict[str, str]) -> None:
        """
        Process a single message outside the consume loop.
        Primarily used in tests and manual invocation.
        """
        self._handle_message(message_id, fields)

    # -- internal dispatch --

    def _handle_message(self, message_id: str, fields: dict[str, str]) -> None:
        msg_type = fields.get("type", "")
        saga_id = fields.get("saga_id", "")
        step_id = fields.get("step_id", "")

        if msg_type == "DispatchStep":
            self._handle_dispatch_step(saga_id, step_id, fields)
        elif msg_type == "DispatchCompensation":
            self._handle_dispatch_compensation(saga_id, step_id, fields)
        else:
            logger.warning(
                "unrecognised message type %r in message %s — skipped",
                msg_type,
                message_id,
            )

    # -- execute path --

    def _handle_dispatch_step(
        self, saga_id: str, step_id: str, fields: dict[str, str]
    ) -> None:
        config = self._tapestry.get(self._step_type)
        if config is None:
            reason = f"no Tapestry config registered for step_type={self._step_type!r}"
            logger.error(reason)
            self._publish_result("StepFailed", saga_id, step_id, reason=reason)
            self._tracker.record_failure(self._step_type)
            return

        try:
            response = call_endpoint(config.execute, fields, self._http)
        except Exception as exc:
            reason = f"HTTP call raised: {exc}"
            logger.exception(
                "execute HTTP call failed: saga=%s step=%s", saga_id, step_id
            )
            self._publish_result("StepFailed", saga_id, step_id, reason=reason)
            self._tracker.record_failure(self._step_type)
            return

        if is_success(response, config.execute_success_check):
            logger.info("step succeeded: saga=%s step=%s", saga_id, step_id)
            self._publish_result("StepSucceeded", saga_id, step_id)
            self._tracker.record_success(self._step_type)
        else:
            reason = (
                f"service returned http_status={response.status_code}; "
                f"body_prefix={response.text()[:200]!r}"
            )
            logger.warning(
                "step failed: saga=%s step=%s %s", saga_id, step_id, reason
            )
            self._publish_result("StepFailed", saga_id, step_id, reason=reason)
            self._tracker.record_failure(self._step_type)

    # -- compensation path --

    def _handle_dispatch_compensation(
        self, saga_id: str, step_id: str, fields: dict[str, str]
    ) -> None:
        config = self._tapestry.get(self._step_type)
        if config is None:
            reason = f"no Tapestry config registered for step_type={self._step_type!r}"
            logger.error(reason)
            self._publish_result(
                "CompensationFailed", saga_id, step_id, reason=reason
            )
            return

        comp = config.compensation
        if comp is None:
            reason = (
                f"step_type={self._step_type!r} has no compensation config; "
                "manual intervention required"
            )
            logger.warning(
                "no compensation config: saga=%s step=%s", saga_id, step_id
            )
            self._publish_result(
                "CompensationFailed", saga_id, step_id, reason=reason
            )
            return

        if comp.requires_manual_review:
            reason = "compensation requires manual review per Tapestry config"
            logger.warning(
                "manual review required: saga=%s step=%s", saga_id, step_id
            )
            self._publish_result(
                "CompensationFailed", saga_id, step_id, reason=reason
            )
            return

        # Automatic compensation call.  Invariant: comp.request is non-None here
        # (enforced by tapestry/loader.py).
        assert comp.request is not None, (
            "loader invariant violated: requires_manual_review=False but request=None"
        )
        success_check = comp.success_check or config.execute_success_check

        try:
            response = call_endpoint(comp.request, fields, self._http)
        except Exception as exc:
            reason = f"compensation HTTP call raised: {exc}"
            logger.exception(
                "compensation HTTP call failed: saga=%s step=%s", saga_id, step_id
            )
            self._publish_result(
                "CompensationFailed", saga_id, step_id, reason=reason
            )
            return

        if is_success(response, success_check):
            logger.info(
                "compensation succeeded: saga=%s step=%s", saga_id, step_id
            )
            self._publish_result("CompensationSucceeded", saga_id, step_id)
        else:
            reason = (
                f"compensation returned http_status={response.status_code}; "
                f"body_prefix={response.text()[:200]!r}"
            )
            logger.warning(
                "compensation failed: saga=%s step=%s %s", saga_id, step_id, reason
            )
            self._publish_result(
                "CompensationFailed", saga_id, step_id, reason=reason
            )

    # -- result publishing --

    def _publish_result(
        self,
        event_type: str,
        saga_id: str,
        step_id: str,
        *,
        reason: str = "",
    ) -> None:
        """
        Publish an outcome event to spindle:results.

        Schema mirrors the engine Event types so the orchestrator's future
        consumer can deserialise directly into StepSucceeded / StepFailed /
        CompensationSucceeded / CompensationFailed:
          type      — event class name (string)
          saga_id   — str
          step_id   — str
          reason    — str, only present for *Failed events
        """
        result_fields: dict[str, str] = {
            "type": event_type,
            "saga_id": saga_id,
            "step_id": step_id,
        }
        if reason:
            result_fields["reason"] = reason
        self._r.xadd(RESULTS_STREAM_KEY, result_fields)
