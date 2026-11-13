from spindle_core.templates.loader import TemplateStore
from spindle_core.templates.models import SagaTemplate, StepRef


def test_empty_store_returns_empty_list() -> None:
    # Verify an empty template store yields an empty list
    store = TemplateStore([])
    assert store.all() == []


def test_all_sorted_by_name_and_version() -> None:
    # Verify templates are returned sorted ascending by name then version
    t_order_v2 = SagaTemplate(name="order", version=2, steps=(StepRef("s1", "t1"),))
    t_alpha_v3 = SagaTemplate(name="alpha", version=3, steps=())
    t_order_v1 = SagaTemplate(name="order", version=1, steps=(StepRef("s1", "t1"),))
    t_alpha_v1 = SagaTemplate(name="alpha", version=1, steps=())

    store = TemplateStore([t_order_v2, t_alpha_v3, t_order_v1, t_alpha_v1])
    result = store.all()

    assert all(isinstance(t, SagaTemplate) for t in result)
    assert result == [t_alpha_v1, t_alpha_v3, t_order_v1, t_order_v2]


def test_all_mutation_safety() -> None:
    # Verify mutating the returned list does not affect subsequent calls
    template = SagaTemplate(name="order", version=1, steps=())
    store = TemplateStore([template])

    first_call = store.all()
    first_call.clear()

    second_call = store.all()
    assert second_call == [template]

