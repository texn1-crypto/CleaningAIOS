from app.agent_evals import load_intent_evals, run_intent_evals


def test_intent_eval_dataset_is_valid_and_green():
    cases = load_intent_evals()
    result = run_intent_evals()

    assert len(cases) >= 30
    assert result["total"] == len(cases)
    assert result["failures"] == []
    assert result["failed"] == 0
    assert result["passed"] == len(cases)
