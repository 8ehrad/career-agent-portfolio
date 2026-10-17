"""Pure, offline paired evaluation; missing labels/predictions never count as failures."""
from __future__ import annotations

from collections import defaultdict

ARMS = ("current", "selected", "whole_corpus")
STRATA = ("gate", "role_family", "source", "salary_state", "location_state")


def confusion(rows):
    tp = sum(truth and prediction for truth, prediction in rows)
    tn = sum(not truth and not prediction for truth, prediction in rows)
    fp = sum(not truth and prediction for truth, prediction in rows)
    fn = sum(truth and not prediction for truth, prediction in rows)
    return {"n": len(rows), "true_positive": tp, "true_negative": tn, "false_positive": fp,
            "false_negative": fn, "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "accuracy": (tp + tn) / len(rows) if rows else None}


def evaluate(cases, labels, predictions):
    cases_by_id = {c["id"]: c for c in cases}
    if len(cases_by_id) != len(cases):
        raise ValueError("Duplicate case IDs")
    reviewed, seen = {}, set()
    for label in labels:
        key = label["case_id"]
        if key not in cases_by_id or key in seen:
            raise ValueError("Unknown or duplicate label case")
        seen.add(key)
        if label.get("decision") not in (None, "unsure", "would_apply", "would_not_apply"):
            raise ValueError("Invalid human decision")
        if label.get("decision") in {"would_apply", "would_not_apply"}:
            if label.get("label_origin") != "human" or not label.get("reviewer"):
                raise ValueError("Accuracy labels require human origin and reviewer attribution")
            if not label.get("job_sha256") or label["job_sha256"] != cases_by_id[key].get("job_sha256"):
                raise ValueError("Human label/vacancy content identity mismatch")
            reviewed[key] = label["decision"] == "would_apply"
    indexed = {}
    for prediction in predictions:
        key = (prediction["case_id"], prediction["arm"])
        if key[0] not in cases_by_id or key[1] not in ARMS or key in indexed:
            raise ValueError("Unknown or duplicate prediction")
        if type(prediction.get("passed")) is not bool:
            raise ValueError("Prediction requires a boolean final-gate decision")
        indexed[key] = prediction
    # Paired comparisons use the same labelled cases in every arm (not different subsets).
    paired = sorted(key for key in reviewed if all((key, arm) in indexed for arm in ARMS))
    arms = {}
    for arm in ARMS:
        available = [key for key in reviewed if (key, arm) in indexed]
        arms[arm] = {"available_labelled": len(available),
                     "paired": confusion([(reviewed[key], indexed[key, arm]["passed"]) for key in paired])}
    regressions = {}
    for arm in ARMS[1:]:
        regressions[arm] = {
            "new_false_positives": [key for key in paired if not reviewed[key]
                                    and not indexed[key, "current"]["passed"] and indexed[key, arm]["passed"]],
            "new_false_negatives": [key for key in paired if reviewed[key]
                                    and indexed[key, "current"]["passed"] and not indexed[key, arm]["passed"]],
            "corrected_cases": [key for key in paired if indexed[key, "current"]["passed"] != reviewed[key]
                                and indexed[key, arm]["passed"] == reviewed[key]]}
    strata = {}
    for dimension in STRATA:
        buckets = defaultdict(list)
        for key in paired:
            buckets[cases_by_id[key]["strata"].get(dimension) or "unknown"].append(key)
        strata[dimension] = {bucket: {arm: confusion([(reviewed[key], indexed[key, arm]["passed"])
                                                     for key in keys]) for arm in ARMS}
                             for bucket, keys in sorted(buckets.items())}
    return {"human_labelled_cases": len(reviewed), "paired_cases": len(paired),
            "excluded_unlabelled_or_unsure": len(cases) - len(reviewed),
            "labelled_missing_paired_predictions": len(reviewed) - len(paired),
            "status": "paired_results_available" if paired else "awaiting_human_labels_and_or_model_responses",
            "arms": arms, "regressions": regressions, "strata": strata,
            "limitation": "Final-fit gate versus stated willingness to apply, not digest top-N precision. Synthetic fixtures are not representative live-vacancy accuracy. Small samples and subjective labels require review; never auto-deploy from this report."}
