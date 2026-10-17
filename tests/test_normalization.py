from __future__ import annotations

import unittest

from career_agent.connectors import deduplicate_jobs
from career_agent.normalization import canonical_url, parse_salary, normalize_job
from career_agent.models import Job
from career_agent.advert_document import prepare_advert, fingerprint
from career_agent.analysis_cache import analysis_identity
from career_agent.enrichment import RuleBasedAnalyzer
from career_agent.profile import load_profile
from tests.helpers import fixture_jobs


class NormalizationTests(unittest.TestCase):
    def test_default_preserves_original_and_optional_heading_boundaries(self):
        raw = {"title": "Data Scientist", "company": "Acme", "location": "UK",
               "description": "<h5>Essential</h5><ul><li>Python</li></ul><h6>Ideally</h6><ol><li>SQL</li></ol>"}
        job = normalize_job(raw, "reed")
        self.assertEqual(raw["description"], job.advert_document["original"])
        self.assertEqual("Essential\n\n• Python\n\nIdeally\n\n• SQL", job.description)
        self.assertEqual(job.to_dict(), Job.from_dict(job.to_dict()).to_dict())
        legacy = normalize_job(raw, "reed", preserve_advert=False)
        self.assertEqual(legacy.id, job.id)
        self.assertEqual(legacy.skills, job.skills)
        self.assertNotEqual(analysis_identity(job, load_profile(), RuleBasedAnalyzer()).cache_key,
                            analysis_identity(legacy, load_profile(), RuleBasedAnalyzer()).cache_key)

    def test_plain_text_newlines_and_inline_html(self):
        raw = "Essential:\r\n- Python\r\n\r\nPreferred:\r\n- SQL"
        self.assertEqual(raw.replace("\r\n", "\n"), normalize_job({"description": raw}, "reed").description)
        self.assertEqual("Python & SQL", prepare_advert("<strong>Python</strong> &amp; <em>SQL</em>")["text"])
        self.assertEqual("A B", prepare_advert("<table><tr><td>A</td><td>B</td></tr></table>")["text"])
        self.assertEqual("Essential\n\n• Python", prepare_advert(
            "&lt;h2&gt;Essential&lt;/h2&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;")["text"])

    def test_original_hash_compatible_and_hidden_content_excluded(self):
        from career_agent.evidence_selection import fingerprint as old_fingerprint
        raw = "<h2>Skills</h2><script>unsafe()</script><style>hidden</style><p>Python</p>"
        document = prepare_advert(raw)
        self.assertEqual(fingerprint(raw), old_fingerprint(raw))
        self.assertEqual(raw, document["original"])
        self.assertNotIn("unsafe", document["text"])
        self.assertNotIn("hidden", document["text"])

    def test_salary_range_in_full_numbers(self):
        salary = parse_salary("Salary £65,000 - £78,000 per annum")
        self.assertEqual(65_000, salary.minimum_gbp)
        self.assertEqual(78_000, salary.maximum_gbp)
        self.assertEqual(71_500, salary.midpoint_gbp)

    def test_salary_range_in_thousands(self):
        salary = parse_salary("£60k to £72k")
        self.assertEqual((60_000, 72_000), (salary.minimum_gbp, salary.maximum_gbp))

    def test_non_salary_number_is_not_parsed(self):
        salary = parse_salary("Work with 1 million customers and 5 years experience")
        self.assertIsNone(salary.midpoint_gbp)

    def test_foreign_currency_and_unrelated_numbers_are_not_parsed_as_gbp(self):
        salary = parse_salary(
            "Over 200 product SKUs. The base salary range is 152,000 USD - 241,500 USD."
        )
        self.assertIsNone(salary.midpoint_gbp)

    def test_unprefixed_salary_range_requires_salary_context(self):
        salary = parse_salary("Annual salary range 60000 to 70000 per year")
        self.assertEqual((60_000, 70_000), (salary.minimum_gbp, salary.maximum_gbp))

    def test_tracking_parameters_are_removed(self):
        value = canonical_url("https://Example.com/job/1/?utm_source=x&ref=abc&keep=yes#top")
        self.assertEqual("https://example.com/job/1?keep=yes", value)

    def test_duplicate_company_suffix_is_collapsed(self):
        jobs = deduplicate_jobs(fixture_jobs())
        northstar = [job for job in jobs if "Northstar" in job.company]
        self.assertEqual(1, len(northstar))


if __name__ == "__main__":
    unittest.main()
