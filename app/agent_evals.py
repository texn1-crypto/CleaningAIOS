from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .chat import understand_russian_message


DEFAULT_EVAL_PATH = Path(__file__).resolve().parents[1] / "evals" / "telegram_intents.json"


def load_intent_evals(path: Path = DEFAULT_EVAL_PATH) -> list[dict[str, Any]]:
    """Load and validate the deterministic Telegram intent golden set."""
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("Intent eval dataset must be a non-empty JSON list")

    seen: set[str] = set()
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ValueError(f"Eval case {index} must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"Eval case {index} has no stable id")
        if case_id in seen:
            raise ValueError(f"Duplicate eval id: {case_id}")
        seen.add(case_id)
        if not isinstance(case.get("message"), str):
            raise ValueError(f"Eval case {case_id} has no message")
        if not isinstance(case.get("expected"), dict) or not case["expected"]:
            raise ValueError(f"Eval case {case_id} has no expected subset")
    return cases


def _subset_errors(expected: dict[str, Any], actual: dict[str, Any], prefix: str = "") -> list[str]:
    errors: list[str] = []
    for key, expected_value in expected.items():
        path = f"{prefix}.{key}" if prefix else key
        if key not in actual:
            errors.append(f"{path}: missing")
            continue
        actual_value = actual[key]
        if isinstance(expected_value, dict):
            if not isinstance(actual_value, dict):
                errors.append(f"{path}: expected object, got {type(actual_value).__name__}")
            else:
                errors.extend(_subset_errors(expected_value, actual_value, path))
        elif actual_value != expected_value:
            errors.append(f"{path}: expected {expected_value!r}, got {actual_value!r}")
    return errors


def evaluate_intent_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate intent routing without a model, network, database or side effect."""
    failures: list[dict[str, Any]] = []
    for case in cases:
        actual = understand_russian_message(
            case["message"],
            referenced_text=case.get("referenced_text", ""),
        )
        errors = _subset_errors(case["expected"], actual)

        # These invariants protect the policy boundary even if a golden case is
        # accidentally weakened in a later edit.
        if actual.get("kind") == "task":
            payload = actual.get("payload", {})
            action_kind = payload.get("action_kind")
            if bool(action_kind) != bool(actual.get("protected")):
                errors.append("protected must exactly match the presence of payload.action_kind")
            serialized = json.dumps(actual, ensure_ascii=False)
            for marker in case.get("must_not_contain", []):
                if marker in serialized:
                    errors.append(f"result contains forbidden marker {marker!r}")

        if errors:
            failures.append({"id": case["id"], "errors": errors, "actual": actual})

    return {
        "total": len(cases),
        "passed": len(cases) - len(failures),
        "failed": len(failures),
        "failures": failures,
    }


def run_intent_evals(path: Path = DEFAULT_EVAL_PATH) -> dict[str, Any]:
    return evaluate_intent_cases(load_intent_evals(path))
