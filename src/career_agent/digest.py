from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from .models import Job, RunReport, VerificationStatus


@dataclass(slots=True)
class Digest:
    subject: str
    html_body: str
    text_body: str


def render_digest(
    jobs: list[Job],
    dashboard_url: str,
    run_date: str,
    *,
    report: RunReport | None = None,
    new_job_ids: set[str] | None = None,
) -> Digest:
    new_job_ids = new_job_ids or set()
    rows: list[str] = []
    text_rows: list[str] = []
    confirmed_jobs = [
        job for job in jobs if job.salary_verification == VerificationStatus.CONFIRMED_PASS
    ]
    unverified_jobs = [
        job for job in jobs if job.salary_verification != VerificationStatus.CONFIRMED_PASS
    ]
    ordered_jobs = confirmed_jobs + unverified_jobs
    previous_section: str | None = None
    for index, job in enumerate(ordered_jobs, start=1):
        section = (
            "Strong matches—salary confirmed"
            if job.salary_verification == VerificationStatus.CONFIRMED_PASS
            else "Strong matches—salary unverified"
        )
        if section != previous_section:
            rows.append(
                '<tr><td style="padding:22px 0 4px;font:700 15px Arial;color:#111827">'
                f"{html.escape(section)}</td></tr>"
            )
            text_rows.append(section.upper())
            previous_section = section
        salary = _salary_label(job)
        location_note = (
            " · UK eligibility unverified"
            if job.location_verification == VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE
            else ""
        )
        reasons = " ".join(job.match_reasons[:2])
        freshness = "NEW" if job.id in new_job_ids else "STILL OPEN"
        detail_url = f"{dashboard_url.rstrip('/')}/career?job={quote(job.id)}"
        rows.append(
            f"""
            <tr><td style="padding:18px 0;border-bottom:1px solid #e5e7eb">
              <div style="font:600 12px Arial;color:#6d28d9">{freshness} · #{index} · {job.score.overall:.0f}/100</div>
              <h2 style="margin:6px 0 2px;font:700 19px Arial;color:#111827">{html.escape(job.title)}</h2>
              <div style="font:14px Arial;color:#4b5563">{html.escape(job.company)} · {html.escape(job.location)} · {html.escape(job.workplace.title())}{html.escape(location_note)}</div>
              <div style="margin-top:8px;font:700 14px Arial;color:#166534">{html.escape(salary)}</div>
              <p style="margin:10px 0;font:14px/1.55 Arial;color:#374151">{html.escape(reasons)}</p>
              <div style="font:12px Arial;color:#6b7280">Attainability {job.score.attainability:.0f} · Career gain {job.score.career_gain:.0f} · Remuneration {job.score.remuneration:.0f}</div>
              <a href="{html.escape(detail_url)}" style="display:inline-block;margin-top:12px;padding:9px 13px;border-radius:7px;background:#111827;color:white;text-decoration:none;font:700 12px Arial">Review in dashboard</a>
            </td></tr>
            """
        )
        text_rows.append(
            f"{index}. [{freshness}] {job.title} — {job.company}\n"
            f"{job.location} · {job.workplace}{location_note} · {salary}\n"
            f"Score {job.score.overall:.0f}/100. {reasons}\n{detail_url}"
        )
    source_summary = ""
    run_summary = ""
    if report is not None:
        source_summary = ", ".join(f"{source}: {count}" for source, count in sorted(report.source_counts.items()))
        stages = report.stage_counts
        new_stages = report.new_stage_counts
        run_summary = (
            f"Scanned {report.discovered:,} listings from {len(report.source_counts)} source types; "
            f"{report.unique:,} unique ({new_stages.get('canonical_unique', 0):,} new to the ledger), "
            f"{report.hard_filter_passed:,} passed initial filters, "
            f"{stages.get('salary_confirmed_pass', 0):,} had confirmed salary above the floor and "
            f"{stages.get('salary_unverified_but_plausible', 0):,} remained plausible with salary unverified, "
            f"{report.deeply_analyzed:,} were deeply analysed, and "
            f"{stages.get('qualified_shortlist', 0):,} reached the qualified shortlist. "
            f"Source/analysis warnings: {len(report.errors)}."
        )
    new_count = sum(job.id in new_job_ids for job in jobs)
    heading = "Your strongest current opportunities" if jobs else "No viable matches this run"
    intro = (
        "New matches are labelled NEW; previously surfaced vacancies that remain among the best current options are labelled STILL OPEN."
        if jobs
        else "The agent completed successfully, but no current vacancy cleared the confirmed-failure and final fit gates."
    )
    source_html = (
        f'<p style="font:12px/1.5 Arial;color:#6b7280">{html.escape(run_summary)}<br>{html.escape(source_summary)}</p>'
        if run_summary
        else ""
    )
    body = f"""
    <!doctype html><html><body style="margin:0;background:#f5f3ff">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:26px 12px">
        <table role="presentation" width="640" cellspacing="0" cellpadding="0" style="max-width:640px;background:white;border-radius:14px;padding:26px">
          <tr><td><div style="font:700 12px Arial;color:#6d28d9;letter-spacing:.08em">CAREER AGENT · {html.escape(run_date)}</div>
          <h1 style="margin:8px 0;font:700 27px Arial;color:#111827">{html.escape(heading)}</h1>
          <p style="font:14px/1.6 Arial;color:#4b5563">{html.escape(intro)} Salary estimates are labelled in the dashboard with their evidence.</p>
          {source_html}</td></tr>
          {''.join(rows)}
          <tr><td style="padding-top:20px;font:12px/1.5 Arial;color:#6b7280">This digest does not apply automatically. Review evidence and approve application materials in the dashboard.</td></tr>
        </table>
      </td></tr></table>
    </body></html>
    """
    return Digest(
        subject=(
            f"Career Agent: {new_count} new, {len(jobs) - new_count} still open — {run_date}"
            if jobs
            else f"Career Agent: run complete, no qualified matches — {run_date}"
        ),
        html_body=body,
        text_body=(
            f"{heading}\n\n{intro}\n{run_summary}\n{source_summary}\n\n" + "\n\n".join(text_rows)
        ).strip(),
    )


def _salary_label(job: Job) -> str:
    salary = job.salary
    if salary.minimum_gbp is None and salary.maximum_gbp is None:
        return "Salary not confidently established"
    if salary.minimum_gbp == salary.maximum_gbp or salary.maximum_gbp is None:
        value = f"£{salary.minimum_gbp or salary.maximum_gbp:,}"
    else:
        value = f"£{salary.minimum_gbp:,}–£{salary.maximum_gbp:,}"
    basis = "estimated" if salary.inferred else "advertised"
    return f"{value} {basis} · {salary.confidence.value} confidence"


class EmailSender(Protocol):
    def send(self, recipient: str, digest: Digest, idempotency_key: str) -> None: ...


class MemoryEmailSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Digest, str]] = []

    def send(self, recipient: str, digest: Digest, idempotency_key: str) -> None:
        if any(key == idempotency_key for _, _, key in self.sent):
            return
        self.sent.append((recipient, digest, idempotency_key))


class SesEmailSender:
    def __init__(self, sender: str, ses_client=None) -> None:
        if ses_client is None:
            import boto3

            ses_client = boto3.client("sesv2")
        self.sender = sender
        self.client = ses_client

    def send(self, recipient: str, digest: Digest, idempotency_key: str) -> None:
        self.client.send_email(
            FromEmailAddress=self.sender,
            Destination={"ToAddresses": [recipient]},
            Content={
                "Simple": {
                    "Subject": {"Data": digest.subject, "Charset": "UTF-8"},
                    "Body": {
                        "Html": {"Data": digest.html_body, "Charset": "UTF-8"},
                        "Text": {"Data": digest.text_body, "Charset": "UTF-8"},
                    },
                    "Headers": [{"Name": "X-Career-Agent-Run", "Value": idempotency_key}],
                }
            },
        )


class SnsEmailSender:
    """Low-cost email delivery for accounts without SES identity permissions."""

    def __init__(self, topic_arn: str, sns_client=None) -> None:
        if sns_client is None:
            import boto3

            sns_client = boto3.client("sns")
        self.topic_arn = topic_arn
        self.client = sns_client

    def send(self, recipient: str, digest: Digest, idempotency_key: str) -> None:
        self.client.publish(
            TopicArn=self.topic_arn,
            Subject=digest.subject[:100],
            Message=digest.text_body,
            MessageAttributes={
                "CareerAgentRun": {"DataType": "String", "StringValue": idempotency_key},
                "IntendedRecipient": {"DataType": "String", "StringValue": recipient},
            },
        )
