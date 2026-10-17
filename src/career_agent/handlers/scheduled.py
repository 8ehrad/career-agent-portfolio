from __future__ import annotations

import json
import logging

from career_agent.handlers.workday_finalize import handle_records
from career_agent.runtime import pipeline


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    if isinstance(event, dict) and event.get("Records"):
        return handle_records(event)
    run_id = event.get("run_id") if isinstance(event, dict) else None
    if isinstance(run_id, str) and "<aws.scheduler." in run_id:
        raise ValueError(f"EventBridge Scheduler context attribute was not resolved: {run_id}")
    # Operators can smoke-test discovery and ranking without consuming the
    # once-per-run digest claim while the email subscription is unconfirmed.
    send_email = bool(event.get("send_email", True)) if isinstance(event, dict) else True
    # Direct/manual invocations remain useful for diagnosis, but Workday is
    # always collected by the queue-backed fan-out to protect this function's timeout.
    report, recommendations = pipeline(excluded_ats={"workday"}).run(
        run_id=run_id, send_email=send_email
    )
    logger.info(json.dumps({"event": "pipeline_completed", **report.to_dict()}))
    return {"report": report.to_dict(), "recommendation_ids": [job.id for job in recommendations]}
