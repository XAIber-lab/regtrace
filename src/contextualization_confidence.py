import re
from dataclasses import dataclass
from typing import Any, Mapping


SUBCATEGORY_RE = re.compile(r"\b(?:DP-)?[A-Z]{2}\.[A-Z]{2}-\d{1,2}\b", re.IGNORECASE)
SUBCATEGORY_ID_RE = re.compile(
    r"(?P<family>(?:DP-)?[A-Z]{2}\.[A-Z]{2})-(?P<number>\d{1,2})",
    re.IGNORECASE,
)

CONFIDENCE_WEIGHTS = {
    "subcategory_in_framework_core": 20,
    "cybersecurity_relevance_coherence": 20,
    "mapping_action_consistency": 15,
    "target_row_validation": 25,
    "no_fallback_or_downgrade": 10,
    "independent_run_agreement": 10,
}
CONFIDENCE_LEVELS = {"high", "medium", "low", "review_needed"}
SELF_CONSISTENCY_STATUSES = {"not_requested", "matched", "not_matched", "unavailable"}
PUBLISHABLE_CONFIDENCE_LEVELS = {"high", "medium"}


@dataclass(frozen=True)
class SelfConsistencyEvidence:
    requested: bool
    action_signatures: dict[str, dict[str, set[str]]]


def value_to_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def canonicalize_subcategory_id(value: Any) -> str:
    text = value_to_text(value)
    match = SUBCATEGORY_ID_RE.fullmatch(text)
    if not match:
        return text
    return f"{match.group('family').upper()}-{int(match.group('number')):02d}"


def subcategory_ids_from_text(value: Any) -> set[str]:
    text = value_to_text(value)
    subcategory_ids = {
        canonicalize_subcategory_id(match.group(0))
        for match in SUBCATEGORY_RE.finditer(text)
    }
    if subcategory_ids:
        return subcategory_ids
    candidate = text.split(":", 1)[0].strip() if ":" in text else text
    candidate = canonicalize_subcategory_id(candidate)
    return {candidate} if SUBCATEGORY_ID_RE.fullmatch(candidate) else set()


def action_subcategory_ids(action: dict[str, Any], action_kind: str) -> set[str]:
    if action_kind == "prototype":
        subcategory_id = canonicalize_subcategory_id(action.get("subcategory_id"))
        return {subcategory_id} if subcategory_id else set()
    return subcategory_ids_from_text(action.get("subcategories_with_weight"))


def action_signature(action: dict[str, Any], action_kind: str) -> str:
    return f"{value_to_text(action.get('action'))}|{','.join(sorted(action_subcategory_ids(action, action_kind)))}"


def build_self_consistency_evidence(data: dict[str, Any]) -> SelfConsistencyEvidence:
    action_signatures: dict[str, dict[str, set[str]]] = {}
    for change in data.get("change_contextualizations", []):
        if not isinstance(change, dict):
            continue
        change_id = value_to_text(change.get("change_id"))
        if not change_id:
            continue
        action_signatures[change_id] = {
            "prototype": {
                action_signature(action, "prototype")
                for action in change.get("prototype_actions", [])
                if isinstance(action, dict) and value_to_text(action.get("action")) != "no_change"
            },
            "control": {
                action_signature(action, "control")
                for action in change.get("control_actions", [])
                if isinstance(action, dict) and value_to_text(action.get("action")) != "no_change"
            },
        }
    return SelfConsistencyEvidence(True, action_signatures)


def confidence_component(criterion: str, earned_points: int, max_points: int) -> dict[str, Any]:
    status = "pass" if earned_points == max_points else "partial" if earned_points else "fail"
    return {
        "criterion": criterion,
        "earned_points": earned_points,
        "max_points": max_points,
        "status": status,
    }


def confidence_level(score: int, effective_action: str) -> str:
    if effective_action == "review_needed":
        return "review_needed"
    if score >= 80:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def score_action_confidence(
    change: dict[str, Any],
    action: dict[str, Any],
    action_kind: str,
    framework_lookup: Mapping[str, Any],
    target_validation_passed: bool,
    requires_review: bool,
    effective_action: str,
    self_consistency: SelfConsistencyEvidence,
) -> dict[str, Any]:
    """Score an action from input-backed validation, not model self-evaluation."""
    action_name = value_to_text(action.get("action"))
    proposed_ids = action_subcategory_ids(action, action_kind)
    mapped_ids = {
        canonicalize_subcategory_id(mapping.get("subcategory_id"))
        for mapping in change.get("framework_mappings", [])
        if isinstance(mapping, dict) and value_to_text(mapping.get("subcategory_id"))
    }

    core_matches = {subcategory_id for subcategory_id in proposed_ids if subcategory_id in framework_lookup}
    core_points = _proportional_points(
        core_matches,
        proposed_ids,
        CONFIDENCE_WEIGHTS["subcategory_in_framework_core"],
    )

    relevance = value_to_text(change.get("cybersecurity_relevance"))
    relevance_points = (
        CONFIDENCE_WEIGHTS["cybersecurity_relevance_coherence"]
        if relevance == "relevant"
        else CONFIDENCE_WEIGHTS["cybersecurity_relevance_coherence"] // 2
        if relevance == "uncertain" and action_name == "review_needed"
        else 0
    )
    mapping_points = _proportional_points(
        proposed_ids.intersection(mapped_ids),
        proposed_ids,
        CONFIDENCE_WEIGHTS["mapping_action_consistency"],
    )

    target_points = (
        CONFIDENCE_WEIGHTS["target_row_validation"] if target_validation_passed else 0
    )
    merge_points = (
        CONFIDENCE_WEIGHTS["no_fallback_or_downgrade"]
        if action_name != "review_needed" and effective_action == action_name and not requires_review
        else 0
    )

    self_consistency_status, self_consistency_bonus = _self_consistency_result(
        change, action, action_kind, self_consistency
    )
    components = [
        confidence_component(
            "subcategory_in_framework_core",
            core_points,
            CONFIDENCE_WEIGHTS["subcategory_in_framework_core"],
        ),
        confidence_component(
            "cybersecurity_relevance_coherence",
            relevance_points,
            CONFIDENCE_WEIGHTS["cybersecurity_relevance_coherence"],
        ),
        confidence_component(
            "mapping_action_consistency",
            mapping_points,
            CONFIDENCE_WEIGHTS["mapping_action_consistency"],
        ),
        confidence_component(
            "target_row_validation",
            target_points,
            CONFIDENCE_WEIGHTS["target_row_validation"],
        ),
        confidence_component(
            "no_fallback_or_downgrade",
            merge_points,
            CONFIDENCE_WEIGHTS["no_fallback_or_downgrade"],
        ),
        confidence_component(
            "independent_run_agreement",
            self_consistency_bonus,
            CONFIDENCE_WEIGHTS["independent_run_agreement"],
        ),
    ]
    deterministic_score = sum(component["earned_points"] for component in components[:-1])
    score = deterministic_score + self_consistency_bonus
    return {
        "score": score,
        "level": confidence_level(score, effective_action),
        "deterministic_score": deterministic_score,
        "self_consistency_bonus": self_consistency_bonus,
        "self_consistency_status": self_consistency_status,
        "components": components,
    }


def clean_action_is_publishable(action: dict[str, Any]) -> bool:
    assessment = action.get("confidence_assessment")
    return isinstance(assessment, dict) and assessment.get("level") in PUBLISHABLE_CONFIDENCE_LEVELS


def action_target_keys(action: dict[str, Any], action_kind: str) -> set[str]:
    """Return stable identifiers for the subcategories and explicit row targeted by an action."""
    keys = {
        f"subcategory:{subcategory_id}"
        for subcategory_id in action_subcategory_ids(action, action_kind)
    }
    row_field = "existing_row" if action_kind == "prototype" else "existing_control_row"
    row_value = value_to_text(action.get(row_field))
    if row_value:
        keys.add(f"row:{row_value.casefold()}")
    return keys


def review_needed_target_keys(data: dict[str, Any], action_kind: str) -> set[str]:
    """Collect targets that must not be published while a related action needs review."""
    action_key = "prototype_actions" if action_kind == "prototype" else "control_actions"
    targets: set[str] = set()
    for change in data.get("change_contextualizations", []):
        if not isinstance(change, dict):
            continue
        for action in change.get(action_key, []):
            assessment = action.get("confidence_assessment") if isinstance(action, dict) else None
            if isinstance(assessment, dict) and assessment.get("level") == "review_needed":
                targets.update(action_target_keys(action, action_kind))
    return targets


def clean_action_conflicts_with_review(
    action: dict[str, Any], action_kind: str, review_needed_targets: set[str]
) -> bool:
    """Whether a publishable action touches a target also touched by a review-needed action."""
    return bool(action_target_keys(action, action_kind).intersection(review_needed_targets))


def confidence_display(assessment: Any) -> str:
    if not isinstance(assessment, dict):
        return "-"
    labels = {"high": "Alta", "medium": "Media", "low": "Bassa", "review_needed": "Da verificare"}
    return f"{assessment.get('score', '-')}/100 ({labels.get(assessment.get('level'), '-')})"


def change_confidence_summary(change: dict[str, Any]) -> str:
    summaries: list[str] = []
    for short_label, action_key in [("P", "prototype_actions"), ("C", "control_actions")]:
        for action in change.get(action_key, []):
            if isinstance(action, dict) and action.get("confidence_assessment"):
                summaries.append(f"{short_label}: {confidence_display(action['confidence_assessment'])}")
    return "; ".join(summaries)


def _proportional_points(matches: set[str], proposed: set[str], maximum: int) -> int:
    if not proposed:
        return 0
    return maximum if matches == proposed else int(maximum * len(matches) / len(proposed))


def _self_consistency_result(
    change: dict[str, Any],
    action: dict[str, Any],
    action_kind: str,
    evidence: SelfConsistencyEvidence,
) -> tuple[str, int]:
    if not evidence.requested:
        return "not_requested", 0
    change_actions = evidence.action_signatures.get(value_to_text(change.get("change_id")))
    if change_actions is None:
        return "unavailable", 0
    if action_signature(action, action_kind) in change_actions.get(action_kind, set()):
        return "matched", CONFIDENCE_WEIGHTS["independent_run_agreement"]
    return "not_matched", 0
