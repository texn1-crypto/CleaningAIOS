from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class PromptRelease:
    """Immutable prompt artifact identified without exposing its content."""

    name: str
    version: str
    content: str
    schema_name: str

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def metadata(self) -> dict[str, str]:
        return {
            "name": self.name,
            "version": self.version,
            "digest": self.digest,
            "schema_name": self.schema_name,
        }


@dataclass(frozen=True)
class PromptDeployment:
    stable: PromptRelease
    candidate: PromptRelease | None = None

    def __post_init__(self) -> None:
        if self.candidate and self.candidate.name != self.stable.name:
            raise ValueError("Stable and candidate prompt names must match")


@dataclass(frozen=True)
class PromptSelection:
    release: PromptRelease
    variant: str
    rollout_bucket: int
    candidate_rollout_percent: int

    def metadata(self) -> dict[str, Any]:
        return {
            **self.release.metadata(),
            "variant": self.variant,
            "rollout_bucket": self.rollout_bucket,
            "candidate_rollout_percent": self.candidate_rollout_percent,
        }


def _canonical_subject(subject: Any) -> str:
    try:
        return json.dumps(subject, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return repr(subject)


def select_prompt(
    deployment: PromptDeployment,
    *,
    subject: Any,
    candidate_rollout_percent: int,
    rollout_seed: str,
) -> PromptSelection:
    """Select a candidate deterministically so retries keep the same prompt."""

    percent = max(0, min(int(candidate_rollout_percent), 100))
    material = (
        f"{rollout_seed}\0{deployment.stable.name}\0{_canonical_subject(subject)}"
    ).encode("utf-8")
    bucket = int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % 100
    use_candidate = deployment.candidate is not None and bucket < percent
    release = deployment.candidate if use_candidate else deployment.stable
    assert release is not None
    return PromptSelection(
        release=release,
        variant="candidate" if use_candidate else "stable",
        rollout_bucket=bucket,
        candidate_rollout_percent=percent,
    )


def deployment_catalog(
    deployments: Mapping[str, PromptDeployment],
    *,
    candidate_rollout_percent: int,
) -> dict[str, Any]:
    """Return deploy metadata only; prompt content is intentionally excluded."""

    percent = max(0, min(int(candidate_rollout_percent), 100))
    rows: list[dict[str, Any]] = []
    for operation, deployment in sorted(deployments.items()):
        rows.append(
            {
                "operation": operation,
                "stable": deployment.stable.metadata(),
                "candidate": (
                    deployment.candidate.metadata() if deployment.candidate else None
                ),
            }
        )
    return {
        "candidate_rollout_percent": percent,
        "rollback": "Set PROMPT_CANDIDATE_ROLLOUT_PERCENT=0 and restart services.",
        "deployments": rows,
    }
