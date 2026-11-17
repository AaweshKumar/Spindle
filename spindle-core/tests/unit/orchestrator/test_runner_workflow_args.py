from spindle_core.engine import SagaState, SagaStatus
from spindle_core.orchestrator.runner import Runner


class _RepoWorkflow:
    def __init__(self):
        self.saved_kwargs: dict = {}

    def load(self, saga_id: str):
        return None

    def save(self, saga_id, new_state, commands, expected_version, **kwargs):
        self.saved_kwargs = {"expected_version": expected_version, **kwargs}


def test_start_with_workflow_args_passes_them_to_save() -> None:
    repo = _RepoWorkflow()
    runner = Runner(repo)
    state = SagaState(saga_id="s1", status=SagaStatus.RUNNING, steps=())
    snapshot = {"name": "order-wf", "version": 2, "definition": {}}
    input_data = {"user": "alice"}

    runner.start(
        state,
        [],
        template_name="order-wf",
        template_version=2,
        source="workflow",
        definition_snapshot=snapshot,
        input_data=input_data,
    )

    assert repo.saved_kwargs["expected_version"] == 0
    assert repo.saved_kwargs["template_name"] == "order-wf"
    assert repo.saved_kwargs["template_version"] == 2
    assert repo.saved_kwargs["source"] == "workflow"
    assert repo.saved_kwargs["definition_snapshot"] == snapshot
    assert repo.saved_kwargs["input_data"] == input_data
