from __future__ import annotations

import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from career_agent.connectors import (
    AdzunaConnector,
    LeverConnector,
    ConnectorError,
    CVLibraryConnector,
    JsonLdConnector,
    PersonioConnector,
    ReedConnector,
    RecruiteeConnector,
    SmartRecruitersConnector,
    SitemapJsonLdConnector,
    TeamtailorConnector,
    WorkableXmlConnector,
    WorkdayBoard,
    WorkdayConnector,
    deduplicate_jobs,
    discover_ats_connectors,
    get_json,
    get_public_text,
    connectors_from_registry,
)
from tests.helpers import ROOT
from career_agent.normalization import normalize_job


class PublicPageFetchTests(unittest.TestCase):
    @patch("career_agent.connectors.urllib.request.urlopen")
    def test_json_fetch_errors_redact_query_credentials(self, urlopen):
        urlopen.side_effect = URLError("network unavailable")

        with self.assertRaises(ConnectorError) as caught:
            get_json("https://api.example.test/search?app_id=public-id&app_key=super-secret&what=data")

        self.assertNotIn("public-id", str(caught.exception))
        self.assertNotIn("super-secret", str(caught.exception))

    def test_rejects_non_http_urls(self):
        with self.assertRaises(ConnectorError):
            get_public_text("file:///etc/passwd")

    def test_rejects_private_and_loopback_addresses(self):
        for url in ("http://127.0.0.1/private", "http://169.254.169.254/latest/meta-data"):
            with self.subTest(url=url), self.assertRaises(ConnectorError):
                get_public_text(url)


class ConnectorMappingTests(unittest.TestCase):
    @patch("career_agent.connectors.get_json")
    def test_lever_preserves_section_titles_and_bullets(self, get_json):
        get_json.return_value = [{"id": "1", "text": "Data Scientist", "descriptionPlain": "Intro",
            "categories": {"location": "UK"}, "hostedUrl": "https://example.test/1",
            "lists": [{"text": "Essential", "content": "<ul><li>Python</li></ul>"},
                      {"text": "Ideally", "content": "<ul><li>SQL</li></ul>"}]}]
        job = LeverConnector(("example",)).fetch()[0]
        self.assertIn("Essential\n\n• Python", job.description)
        self.assertIn("Ideally\n\n• SQL", job.description)
        self.assertIn("<li>SQL</li>", job.advert_document["original"])

    @patch("career_agent.connectors.get_json")
    def test_adzuna_uses_title_search_and_paginates_without_false_uk_location(self, get_json):
        def result(source_id, title, predicted=0):
            return {
                "id": source_id,
                "title": title,
                "company": {"display_name": "Example"},
                "location": {"display_name": "Bristol"},
                "description": "Python machine learning",
                "redirect_url": f"https://example.test/jobs/{source_id}",
                "salary_min": 60_000,
                "salary_max": 80_000,
                "salary_is_predicted": predicted,
            }

        get_json.side_effect = [
            {"count": 3, "results": [result("1", "Data Scientist"), result("2", "Senior Data Scientist")]},
            {"count": 3, "results": [result("3", "Applied Data Scientist", predicted=1)]},
        ]

        jobs = AdzunaConnector(
            "app-id",
            "app-key",
            ("data scientist",),
            results_per_page=2,
            max_results_per_query=3,
            minimum_request_interval_seconds=0,
        ).fetch()

        first_url = get_json.call_args_list[0].args[0]
        second_url = get_json.call_args_list[1].args[0]
        params = parse_qs(urlsplit(first_url).query)
        self.assertEqual("data scientist", params["title_only"][0])
        self.assertNotIn("where", params)
        self.assertIn("/search/1", first_url)
        self.assertIn("/search/2", second_url)
        self.assertEqual(3, len(jobs))
        self.assertTrue(jobs[-1].salary.inferred)

    @patch("career_agent.connectors.get_json")
    def test_adzuna_exposes_documented_optional_search_parameters(self, get_json):
        get_json.return_value = {"count": 0, "results": []}

        connector = AdzunaConnector(
            "app-id",
            "app-key",
            ("data scientist",),
            query_parameter="what_phrase",
            location="Bristol",
            distance_km=50,
            category="it-jobs",
            salary_min=50_000,
            salary_max=120_000,
            salary_include_unknown=True,
            full_time=True,
            part_time=False,
            contract=True,
            permanent=True,
            company="Example",
            what_exclude="intern",
            minimum_request_interval_seconds=0,
        )
        connector.fetch()

        params = parse_qs(urlsplit(get_json.call_args.args[0]).query)
        self.assertEqual("data scientist", params["what_phrase"][0])
        self.assertEqual("Bristol", params["where"][0])
        self.assertEqual("50", params["distance"][0])
        self.assertEqual("1", params["salary_include_unknown"][0])
        self.assertEqual("0", params["part_time"][0])
        self.assertEqual("intern", params["what_exclude"][0])

    @patch("career_agent.connectors.get_json")
    def test_reed_maps_search_results_and_uses_basic_auth(self, get_json):
        get_json.side_effect = [
            {
                "totalResults": 1,
                "results": [
                    {
                        "jobId": 42,
                        "jobTitle": "AI Engineer",
                        "employerName": "Example Bank",
                        "locationName": "Bristol",
                        "jobDescription": "Short search description",
                        "jobUrl": "https://www.reed.co.uk/jobs/42",
                        "minimumSalary": 300,
                        "maximumSalary": 400,
                    }
                ],
            },
            {
                "jobId": 42,
                "jobTitle": "AI Engineer",
                "employerName": "Example Bank",
                "locationName": "Bristol",
                "jobDescription": "Full UK hybrid description with Python.",
                "jobUrl": "https://www.reed.co.uk/jobs/42",
                "externalUrl": "https://example.test/jobs/42",
                "yearlyMinimumSalary": 70_000,
                "yearlyMaximumSalary": 80_000,
                "currency": "GBP",
                "contractType": "Permanent",
                "fullTime": True,
            },
        ]

        jobs = ReedConnector("secret", ("ai engineer",)).fetch()

        self.assertEqual("Example Bank", jobs[0].company)
        self.assertEqual(75_000, jobs[0].salary.midpoint_gbp)
        self.assertEqual("https://example.test/jobs/42", jobs[0].apply_url)
        self.assertIn("Full UK hybrid description", jobs[0].description)
        self.assertTrue(get_json.call_args_list[0].kwargs["headers"]["Authorization"].startswith("Basic "))
        self.assertIn("/api/1.0/jobs/42", get_json.call_args_list[1].args[0])

    @patch("career_agent.connectors.get_json")
    def test_reed_exposes_documented_search_parameters(self, get_json):
        get_json.return_value = {"totalResults": 0, "results": []}

        connector = ReedConnector(
            "secret",
            ("data scientist",),
            employer_profile_id=7,
            location="Bristol",
            distance_miles=50,
            permanent=True,
            contract=True,
            temp=False,
            part_time=False,
            full_time=True,
            minimum_salary=50_000,
            maximum_salary=150_000,
            posted_by_recruitment_agency=True,
            posted_by_direct_employer=True,
            graduate=False,
        )
        connector.fetch()

        params = parse_qs(urlsplit(get_json.call_args.args[0]).query)
        self.assertEqual("7", params["employerProfileId"][0])
        self.assertEqual("50", params["distanceFromLocation"][0])
        self.assertEqual("true", params["permanent"][0])
        self.assertEqual("false", params["temp"][0])
        self.assertEqual("50000", params["minimumSalary"][0])
        self.assertEqual("true", params["postedByDirectEmployer"][0])

    @patch("career_agent.connectors.get_json")
    def test_smartrecruiters_fetches_posting_detail(self, get_json):
        get_json.side_effect = [
            {"content": [{"uuid": "abc", "name": "Data Scientist"}], "totalFound": 1},
            {
                "uuid": "abc",
                "name": "Data Scientist",
                "company": {"name": "Acme"},
                "location": {"city": "London", "country": "GB"},
                "jobAd": {"sections": {"jobDescription": {"title": "Essential", "text": "UK role with Python"},
                                       "qualifications": {"title": "Ideally", "text": "SQL"}}},
                "ref": "https://jobs.smartrecruiters.com/Acme/abc",
            },
        ]

        jobs = SmartRecruitersConnector(("Acme",), queries=("data scientist",)).fetch()

        self.assertEqual(1, len(jobs))
        self.assertIn("Python", jobs[0].skills)
        self.assertIn("Essential\nUK role with Python\n\nIdeally\nSQL", jobs[0].description)

    @patch("career_agent.connectors.get_json")
    @patch("career_agent.connectors.post_json")
    def test_workday_maps_search_and_detail(self, post_json, get_json):
        post_json.return_value = {
            "jobPostings": [{"title": "Machine Learning Engineer", "externalPath": "/job/REQ-1"}],
            "total": 1,
        }
        get_json.return_value = {
            "jobPostingInfo": {
                "jobReqId": "REQ-1",
                "title": "Machine Learning Engineer",
                "company": "Acme Bank",
                "location": "Bristol, UK",
                "jobDescription": "Hybrid machine learning role. Salary £70k-£80k.",
                "externalUrl": "https://acme.wd3.myworkdayjobs.com/job/REQ-1",
            }
        }

        board = WorkdayBoard.parse("acme|wd3|Careers")
        jobs = WorkdayConnector((board,), queries=("machine learning engineer",)).fetch()

        self.assertEqual("acme.wd3.myworkdayjobs.com", board.host)
        self.assertEqual("Acme Bank", jobs[0].company)
        self.assertEqual(75_000, jobs[0].salary.midpoint_gbp)

    @patch("career_agent.connectors.get_text")
    def test_workable_and_personio_parse_documented_xml_shapes(self, get_text):
        get_text.side_effect = [
            (
                "https://www.workable.com/boards/workable.xml",
                "<jobs><job><id>1</id><title>AI Engineer</title><company>Acme</company>"
                "<city>London</city><country>UK</country><description>Python</description>"
                "<url>https://apply.workable.com/acme/j/1</url></job></jobs>",
            ),
            (
                "https://acme.jobs.personio.de/xml?language=en",
                "<positions><position><id>2</id><name>Data Scientist</name><office>Bristol, UK</office>"
                "<jobDescription><section>Python and SQL</section></jobDescription></position></positions>",
            ),
        ]

        workable = WorkableXmlConnector().fetch()
        personio = PersonioConnector(("acme",)).fetch()

        self.assertEqual("Acme", workable[0].company)
        self.assertEqual("Data Scientist", personio[0].title)

    @patch("career_agent.connectors.get_json")
    def test_recruitee_maps_public_offers(self, get_json):
        get_json.return_value = {
            "offers": [
                {
                    "id": 7,
                    "title": "Applied Scientist",
                    "company_name": "Acme",
                    "location": "London, UK",
                    "description": "Applied ML",
                    "careers_url": "https://acme.recruitee.com/o/applied-scientist",
                }
            ]
        }

        jobs = RecruiteeConnector(("acme",)).fetch()

        self.assertEqual("Applied Scientist", jobs[0].title)

    @patch("career_agent.connectors.get_public_text")
    def test_json_ld_extracts_job_posting(self, get_public_text):
        get_public_text.return_value = (
            "https://acme.example/careers",
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","title":"Data Scientist","description":"UK remote Python",'
            '"hiringOrganization":{"name":"Acme"},"jobLocation":{"address":'
            '{"addressLocality":"Bristol","addressCountry":"UK"}},'
            '"url":"https://acme.example/jobs/1"}</script>',
        )

        jobs = JsonLdConnector(("https://acme.example/careers",)).fetch()

        self.assertEqual("Bristol, UK", jobs[0].location)

    @patch("career_agent.connectors.get_public_text")
    @patch("career_agent.connectors.get_text")
    def test_sitemap_discovers_job_pages_then_reads_json_ld(self, get_text, get_public_text):
        get_text.return_value = (
            "https://acme.example/sitemap.xml",
            "<urlset><url><loc>https://acme.example/jobs/1</loc></url>"
            "<url><loc>https://acme.example/about</loc></url></urlset>",
        )
        get_public_text.return_value = (
            "https://acme.example/jobs/1",
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","title":"NLP Engineer","description":"Python NLP",'
            '"hiringOrganization":{"name":"Acme"},"jobLocationType":"TELECOMMUTE",'
            '"applicantLocationRequirements":{"name":"United Kingdom"},'
            '"url":"https://acme.example/jobs/1"}</script>',
        )

        jobs = SitemapJsonLdConnector(("https://acme.example/sitemap.xml",)).fetch()

        self.assertEqual(1, len(jobs))
        self.assertEqual("Remote — United Kingdom", jobs[0].location)
        get_public_text.assert_called_once_with("https://acme.example/jobs/1")

    @patch("career_agent.connectors.get_text")
    def test_teamtailor_reads_the_official_public_rss_feed(self, get_text):
        get_text.return_value = (
            "https://acme.teamtailor.com/jobs.rss",
            '<rss xmlns:tt="https://teamtailor.com/locations"><channel>'
            "<title>Acme</title><item><guid>job-1</guid><title>ML Scientist</title>"
            "<link>https://acme.teamtailor.com/jobs/1-ml-scientist</link>"
            "<description><![CDATA[Python <b>machine learning</b>]]></description>"
            "<pubDate>Fri, 04 Sep 2026 09:30:04 +0100</pubDate>"
            "<remoteStatus>hybrid</remoteStatus><tt:locations><tt:location>"
            "<tt:city>London</tt:city><tt:country>United Kingdom</tt:country>"
            "</tt:location></tt:locations></item></channel></rss>",
        )

        jobs = TeamtailorConnector(("acme",)).fetch()

        self.assertEqual(1, len(jobs))
        self.assertEqual("Acme", jobs[0].company)
        self.assertEqual("London, United Kingdom", jobs[0].location)
        self.assertIn("machine learning", jobs[0].description)

    def test_cv_library_requires_explicit_partner_approval(self):
        with self.assertRaisesRegex(ValueError, "partner approval"):
            CVLibraryConnector("key", ("data scientist",), partner_approved=False)



    def test_discovery_and_deduplication_preserve_listing_provenance(self):
        aggregator = normalize_job(
            {
                "id": "board-result",
                "title": "AI Engineer",
                "company": "Acme",
                "location": "London, UK",
                "description": "UK role",
                "url": "https://jobs.lever.co/acme/123",
            },
            "reed",
        )
        direct = normalize_job(
            {
                "id": "123",
                "title": "AI Engineer",
                "company": "Acme Ltd",
                "location": "London, UK",
                "description": "UK role",
                "url": "https://jobs.lever.co/acme/123",
            },
            "lever",
        )

        discovered = discover_ats_connectors([aggregator], [])
        merged = deduplicate_jobs([aggregator, direct])

        self.assertEqual(("acme",), discovered[0].site_names)
        self.assertEqual(1, len(merged))
        self.assertEqual({"reed", "lever"}, {listing.source for listing in merged[0].listings})

    def test_discovery_recognises_smartrecruiters_and_workday_apply_urls(self):
        smart = normalize_job(
            {
                "title": "ML Scientist",
                "company": "Acme",
                "location": "London, UK",
                "url": "https://jobs.smartrecruiters.com/Acme/123-ml-scientist",
            },
            "reed",
        )
        workday = normalize_job(
            {
                "title": "AI Consultant",
                "company": "Example",
                "location": "London, UK",
                "url": "https://example.wd3.myworkdayjobs.com/en-US/Careers/job/London/AI-Consultant_REQ-1",
            },
            "adzuna",
        )

        discovered = discover_ats_connectors([smart, workday], [])

        smart_connector = next(item for item in discovered if item.name == "smartrecruiters_discovered")
        workday_connector = next(item for item in discovered if item.name == "workday_discovered")
        self.assertEqual(("Acme",), smart_connector.company_ids)
        self.assertEqual("Careers", workday_connector.boards[0].site)


if __name__ == "__main__":
    unittest.main()
