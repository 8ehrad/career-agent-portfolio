from career_agent.destination_lookup import classify_destination


def test_classifies_direct_ats_and_reed_intermediary() -> None:
    workday = classify_destination(
        "https://mimecast.wd5.myworkdayjobs.com/mimecast-careers/job/London/Engineer_R6709-1?utm_source=adzuna"
    )
    assert (workday.kind, workday.platform) == ("ats", "workday")
    assert workday.url == "https://mimecast.wd5.myworkdayjobs.com/mimecast-careers/job/London/Engineer_R6709-1"

    reed = classify_destination(
        "https://reedq.reed.co.uk/clicktracker?destination=https%3A%2F%2Fwww.reed.co.uk%2Fjobs%2Frole%2F57350351%3Futm_source%3DAdzuna&hash=abc"
    )
    assert (reed.kind, reed.platform, reed.url) == (
        "intermediary", "reed", "https://www.reed.co.uk/jobs/role/57350351"
    )


def test_rejects_malformed_or_unsafe_urls() -> None:
    for url in (
        "https://https//apply.careers.microsoft.com/careers/job/1970393556998381",
        "http://jobs.lever.co/pigment/posting",
        "https://127.0.0.1/secret",
        "https://user:pass@jobs.lever.co/pigment/posting",
        "https://reedq.reed.co.uk/clicktracker?hash=abc",
    ):
        assert classify_destination(url).kind == "malformed"


def test_does_not_guess_platform_from_unknown_employer_site() -> None:
    result = classify_destination("https://careers.example.com/job/123")
    assert (result.kind, result.platform) == ("other", None)
