from spindle_core.schema.inputs import missing_inputs, referenced_input_paths
from spindle_core.schema.models import Action, Compensation, SagaDefinition, Step


def _make_definition() -> SagaDefinition:
    step1 = Step(
        name="step1",
        service="order-svc",
        action=Action(
            method="POST",
            path="/orders/{{input.order.id}}",
            body={"user_id": "{{input.user.id}}", "amount": "{{input.payment.amount}}"},
        ),
        compensation=Compensation(
            method="DELETE",
            path="/orders/{{input.order.id}}/cancel",
            body={"reason": "{{input.cancel_reason}}"},
        ),
        timeout_s=5.0,
    )
    step2 = Step(
        name="step2",
        service="notify-svc",
        action=Action(
            method="POST",
            path="/notify/{{input.user.id}}",
            body={"msg": "created", "saga": "{{saga.id}}", "resp": "{{steps.step1.response.token}}"},
        ),
        timeout_s=5.0,
    )
    return SagaDefinition(name="test-workflow", steps=[step1, step2])


def test_referenced_input_paths():
    defn = _make_definition()
    paths = referenced_input_paths(defn)

    # Order preserved, deduplicated: ("user", "id") and ("order", "id") appear once
    expected = [
        ("order", "id"),
        ("user", "id"),
        ("payment", "amount"),
        ("cancel_reason"),  # wait: ("cancel_reason",)
    ]
    assert paths == [
        ("order", "id"),
        ("user", "id"),
        ("payment", "amount"),
        ("cancel_reason",),
    ]


def test_missing_inputs_all_present():
    defn = _make_definition()
    payload = {
        "order": {"id": "ord-123"},
        "user": {"id": "usr-456"},
        "payment": {"amount": 99.5},
        "cancel_reason": "timeout",
    }
    errors = missing_inputs(defn, payload)
    assert errors == []


def test_missing_inputs_nested_and_compensation():
    defn = _make_definition()
    # Missing payment.amount and cancel_reason
    payload = {
        "order": {"id": "ord-123"},
        "user": {"id": "usr-456"},
    }
    errors = missing_inputs(defn, payload)
    assert len(errors) == 2
    assert errors[0].path == "input.payment.amount"
    assert errors[0].message == "required input missing"
    assert errors[1].path == "input.cancel_reason"
    assert errors[1].message == "required input missing"

    # Verify no value is echoed
    for err in errors:
        assert "ord-123" not in str(err)
        assert "usr-456" not in str(err)


def test_missing_inputs_empty_or_none():
    defn = _make_definition()
    errors = missing_inputs(defn, {})
    assert len(errors) == 4
    paths = [e.path for e in errors]
    assert paths == [
        "input.order.id",
        "input.user.id",
        "input.payment.amount",
        "input.cancel_reason",
    ]


def test_missing_inputs_explicit_null_and_list_index() -> None:
    # Pinning behavior for explicit null input value and list-index paths
    step = Step(
        name="step1",
        service="order-svc",
        action=Action(
            method="POST",
            path="/items/{{input.items.0.sku}}",
            body={"customer": "{{input.customer}}"},
        ),
        timeout_s=5.0,
        non_compensable=True,
    )
    defn = SagaDefinition(name="list-and-null-wf", steps=[step])

    paths = referenced_input_paths(defn)
    assert paths == [("items", "0", "sku"), ("customer",)]

    # 1. When explicit null is provided for customer, and valid list item exists
    payload_valid = {
        "customer": None,
        "items": [{"sku": "SKU-ABC-123"}],
    }
    assert missing_inputs(defn, payload_valid) == []

    # 2. When items list is empty
    payload_empty_items = {
        "customer": None,
        "items": [],
    }
    errs = missing_inputs(defn, payload_empty_items)
    assert len(errs) == 1
    assert errs[0].path == "input.items.0.sku"
    assert errs[0].message == "required input missing"

    # 3. When customer key is completely missing
    payload_missing_customer = {
        "items": [{"sku": "SKU-ABC-123"}],
    }
    errs_cust = missing_inputs(defn, payload_missing_customer)
    assert len(errs_cust) == 1
    assert errs_cust[0].path == "input.customer"
    assert errs_cust[0].message == "required input missing"
