"""Routing trace observation helpers extracted from RequestCoordinator."""

from __future__ import annotations

from typing import Any


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    """Return the ratio or None when the denominator is zero."""
    if denominator == 0:
        return None
    return numerator / denominator


def build_top_candidates(
    ranked_candidates: list[tuple[Any, Any]],
    *,
    limit: int = 5,
    fairness_band_names: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Render the top-N ranked candidates for the dashboard table.

    Each entry includes ``rank_before_fairness`` (the candidate's
    position in the score-ordered list before the fairness rotor
    reordered the band), ``rank_after_fairness`` (the candidate's
    position in the final list), and ``fairness_band_member`` (True
    when the candidate was part of the fairness band eligible for
    rotation).
    """
    band = fairness_band_names or frozenset()

    # Build the pre-fairness ordering.  Non-band members keep their
    # post-fairness rank.  Band members are restored to the sorted
    # (by account name) order the rotor used before rotation.
    band_entries = [
        (state, score) for state, score in ranked_candidates if state.name in band
    ]
    band_sorted = sorted(band_entries, key=lambda pair: pair[0].name)
    pre_fairness_rank: dict[str, int] = {}
    band_idx = 0
    for rank, (state, _score) in enumerate(ranked_candidates):
        if state.name in band:
            # Place this band member at its sorted position
            if band_idx < len(band_sorted):
                pre_name = band_sorted[band_idx][0].name
                pre_fairness_rank[pre_name] = rank
                band_idx += 1
        else:
            pre_fairness_rank[state.name] = rank

    out: list[dict[str, Any]] = []
    for rank_after, (state, score) in enumerate(ranked_candidates[:limit]):
        entry: dict[str, Any] = {
            "account_name": state.name,
            "final_score": float(score.final_score),
            "quota_score": score.quota_score,
            "inflight_penalty": score.inflight_penalty,
            "health_penalty": score.health_penalty,
            "tier": int(score.tier),
            "requires_transcode": bool(score.requires_transcode),
            "rank_before_fairness": pre_fairness_rank.get(state.name, rank_after),
            "rank_after_fairness": rank_after,
            "fairness_band_member": state.name in band,
        }
        out.append(entry)
    return out


def derive_tie_break_summary(
    *,
    ranked_candidates: list[tuple[Any, Any]],
    selected_score_obj: Any | None,
) -> dict[str, Any]:
    """Summarise why the selected account won over its runner-up.

    Returns a small dict the dashboard can render inline so
    operators do not have to recompute scores to see whether
    skew was driven by tier, quota utilization, in-flight
    pressure, or a near-tie within the scorer's tiebreaker
    range.
    """
    summary: dict[str, Any] = {
        "factor": "no_runner_up",
        "margin": None,
        "runner_up": None,
    }
    if selected_score_obj is None or len(ranked_candidates) < 2:
        return summary
    # Skip the selected account when searching for the runner-up;
    # the selected entry may not be ranked first if the caller
    # passed a list that does not start with the selected account.
    runner_up: tuple[Any, Any] | None = None
    for state, score in ranked_candidates:
        if state.name == selected_score_obj.account_name:
            continue
        runner_up = (state, score)
        break
    if runner_up is None:
        return summary
    ru_state, ru_score = runner_up
    selected_final = float(selected_score_obj.final_score)
    runner_final = float(ru_score.final_score)
    margin = runner_final - selected_final
    summary["margin"] = margin
    summary["runner_up"] = {
        "account_name": ru_state.name,
        "final_score": runner_final,
        "tier": int(ru_score.tier),
        "requires_transcode": bool(ru_score.requires_transcode),
    }
    if selected_score_obj.requires_transcode != ru_score.requires_transcode:
        summary["factor"] = (
            "transcode" if not selected_score_obj.requires_transcode else "transcode"
        )
        return summary
    if selected_score_obj.tier != ru_score.tier:
        summary["factor"] = "tier"
        return summary
    if margin == 0.0:
        summary["factor"] = "exact_tie"
        return summary
    # Score margins within the scorer's tiebreaker range are not
    # signal — they are deterministic or random noise.  Anything
    # outside the band is a real utilization / penalty delta.
    if abs(margin) <= 0.01:
        summary["factor"] = "near_tie"
        return summary
    if abs(selected_score_obj.inflight_penalty - ru_score.inflight_penalty) > abs(
        selected_score_obj.quota_score - ru_score.quota_score
    ):
        summary["factor"] = "inflight"
        return summary
    summary["factor"] = "quota"
    return summary


def build_score_components(
    *,
    ranked_candidates: list[tuple[Any, Any]],
    selected_account_name: str,
    selected_state: Any,
    selected_score: float | None,
    selected_tier: int | None,
    fairness_decision: Any | None = None,
    fairness_band_names: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Build the score_components_json payload for one routing decision.

    Includes the full breakdown for the selected account plus
    the top near-tie candidates so the dashboard can answer
    "why account A over account B?" without rescoring.

    The payload also carries utilization ratios for each quota
    window (5h/7d/30d) and a short ``tie_break`` summary
    identifying the decisive factor between the chosen account
    and the runner-up (``tier``, ``quota``, ``inflight``,
    ``transcode``, ``near_tie``) so an operator can correlate
    visible skew against a concrete cause.
    """
    # Find the score for the selected account from ranked_candidates
    # if present; else synthesize the bare minimum from the trace.
    selected_score_obj: Any | None = None
    for state, score in ranked_candidates:
        if state.name == selected_account_name:
            selected_score_obj = score
            break

    top_candidates_payload = build_top_candidates(
        ranked_candidates, fairness_band_names=fairness_band_names
    )
    tie_break = derive_tie_break_summary(
        ranked_candidates=ranked_candidates,
        selected_score_obj=selected_score_obj,
    )

    if selected_score_obj is not None:
        payload: dict[str, Any] = {
            "account_name": selected_account_name,
            "quota_score": selected_score_obj.quota_score,
            "inflight_penalty": selected_score_obj.inflight_penalty,
            "health_penalty": selected_score_obj.health_penalty,
            "final_score": selected_score_obj.final_score,
            "weight": selected_score_obj.weight,
            "active_request_count": (selected_score_obj.active_request_count),
            "reserved_microdollars": (selected_score_obj.reserved_microdollars),
            "cost_5h_microdollars": (selected_score_obj.cost_5h_microdollars),
            "cost_7d_microdollars": (selected_score_obj.cost_7d_microdollars),
            "cost_30d_microdollars": (selected_score_obj.cost_30d_microdollars),
            "capacity_5h_microdollars": (selected_score_obj.capacity_5h_microdollars),
            "capacity_7d_microdollars": (selected_score_obj.capacity_7d_microdollars),
            "capacity_30d_microdollars": (selected_score_obj.capacity_30d_microdollars),
            "tier": selected_score_obj.tier,
            "requires_transcode": selected_score_obj.requires_transcode,
            "util_5h": _safe_ratio(
                selected_score_obj.cost_5h_microdollars,
                selected_score_obj.capacity_5h_microdollars,
            ),
            "util_7d": _safe_ratio(
                selected_score_obj.cost_7d_microdollars,
                selected_score_obj.capacity_7d_microdollars,
            ),
            "util_30d": _safe_ratio(
                selected_score_obj.cost_30d_microdollars,
                selected_score_obj.capacity_30d_microdollars,
            ),
            "tie_break": tie_break,
            "top_candidates": top_candidates_payload,
            "fairness": (
                fairness_decision.to_dict() if fairness_decision is not None else None
            ),
        }
    else:
        payload = {
            "account_name": selected_account_name,
            "quota_score": 0.0,
            "inflight_penalty": 0.0,
            "health_penalty": 0.0,
            "final_score": (
                float(selected_score) if selected_score is not None else 0.0
            ),
            "weight": 0.0,
            "active_request_count": 0,
            "reserved_microdollars": 0,
            "cost_5h_microdollars": 0,
            "cost_7d_microdollars": 0,
            "cost_30d_microdollars": 0,
            "capacity_5h_microdollars": 0,
            "capacity_7d_microdollars": 0,
            "capacity_30d_microdollars": 0,
            "tier": int(selected_tier) if selected_tier is not None else 0,
            "requires_transcode": False,
            "util_5h": None,
            "util_7d": None,
            "util_30d": None,
            "tie_break": tie_break,
            "top_candidates": top_candidates_payload,
            "fairness": (
                fairness_decision.to_dict() if fairness_decision is not None else None
            ),
        }
    return payload
