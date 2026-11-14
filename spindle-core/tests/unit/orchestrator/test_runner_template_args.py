from spindle_core.engine import Command, SagaState, SagaStatus
from spindle_core.engine.saga import StepState
from spindle_core.engine.states import StepStatus
from spindle_core.orchestrator.runner import Runner


class _RepoWithTemplate:
    """Fake that accepts template_name and template_version kwargs."""

    def __init__(self):
        self.saved_kwargs: dict = {}

    def load(self, saga_id: str):
        return None

    def save(self, saga_id, new_state, commands, expected_version, *, template_name=None, template_version=None):
        self.saved_kwargs = {
            "expected_version": expected_version,
            "template_name": template_name,
            "template_version": template_version,
        }


class _RepoNoTemplate:
    """Fake with old 4-arg save; extra kwargs raise TypeError."""

    def __init__(self):
        self.saved = False

    def load(self, saga_id: str):
        return None

    def save(self, saga_id, new_state, commands, expected_version):
        self.saved = True


def _simple_state() -> SagaState:
    return SagaState(saga_id="s1", status=SagaStatus.RUNNING, steps=())


def test_start_with_template_args_passes_them_to_save() -> None:
    # Verify template metadata is forwarded to repo.save on creation
    repo = _RepoWithTemplate()
    runner = Runner(repo)
    state = _simple_state()
    runner.start(state, [], template_name="order-fulfillment", template_version=1)
    assert repo.saved_kwargs["expected_version"] == 0
    assert repo.saved_kwargs["template_name"] == "order-fulfillment"
    assert repo.saved_kwargs["template_version"] == 1


def test_start_without_template_args_uses_positional_save() -> None:
    # Verify old-style fakes keep working when no template kwargs given
    repo = _RepoNoTemplate()
    runner = Runner(repo)
    state = _simple_state()
    runner.start(state, [])
    assert repo.saved


def test_start_exactly_one_template_arg_raises_value_error() -> None:
    # Verify mixed (one None, one set) template args are rejected before save
    repo = _RepoNoTemplate()
    runner = Runner(repo)
    state = _simple_state()
    try:
        runner.start(state, [], template_name="order-fulfillment")
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert not repo.saved

    try:
        runner.start(state, [], template_version=1)
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert not repo.saved


def test_handle_event_does_not_pass_template_keywords() -> None:
    # Verify handle_event never passes template kwargs; also covers duplicate no-op
    from spindle_core.engine import decide
    from spindle_core.engine.messages import StepSucceeded
    from spindle_core.orchestrator.ports import LoadedSaga

    class _RepoForEvent(_RepoNoTemplate):
        def load(self, saga_id: str):
            state = SagaState(
                saga_id="s1",
                status=SagaStatus.COMPLETED,
                steps=(StepState("step1", "TypeA", StepStatus.SUCCEEDED),),
            )
            return LoadedSaga(state=state, version=1)

    repo = _RepoForEvent()
    runner = Runner(repo)
    # duplicate event: state unchanged, no save called
    event = StepSucceeded(saga_id="s1", step_id="step1")
    runner.handle_event("s1", event)
    assert not repo.saved

