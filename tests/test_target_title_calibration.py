"""Regression coverage for calibrated, code-free resume target titles."""
from __future__ import annotations

import unittest

from jobhunter.config import load_profile
from jobhunter.models import Job
from jobhunter.resume.tailor import _clean_job_title_for_headline, _safe_target_title


def _job(title: str) -> Job:
    return Job(source="manual", title=title, company="Example Co",
               url="https://example.test/job", description=title).normalize()


class TargetTitleCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = load_profile()

    def _target(self, title: str) -> str:
        return _safe_target_title(self.profile, _job(title))

    def test_accenture_banking_analyst_loses_corporate_code(self):
        title = self._target("GN-I&E-Banking-Ops Excellence-Analyst")
        self.assertNotIn("GN", title)
        self.assertNotEqual(title, "GN-I&E-Banking-Ops Excellence-Analyst")
        self.assertIn("Banking", title)
        self.assertIn("Operations", title)  # Ops expanded

    def test_stripped_taxonomy_falls_back_with_level(self):
        title = self._target("S&C GN - MC - Industry X - Digital Engineering R&D – Analyst")
        self.assertNotIn("Industry X", title)
        self.assertIn("Analyst", title)

    def test_process_excellence_manager_is_clean(self):
        title = self._target("GN-SC&E-PRH-Process Excellence-Manager")
        self.assertNotIn("PRH", title)
        self.assertNotIn("SC&E", title)
        self.assertIn("Process Excellence", title)

    def test_plain_roles_stay_grounded(self):
        self.assertIn("Supply Chain Operations", self._target("Sr. Supply Chain Operations Manager"))

    def test_no_internal_codes_leak(self):
        for raw in ["GN-FOC-Strategy & Operations Manager",
                    "FY24 Supply Chain Program Manager",
                    "Req ID R00344804 Business Operations Analyst"]:
            title = self._target(raw)
            self.assertNotIn("GN-FOC", title)
            self.assertNotIn("FY24", title)
            self.assertNotIn("R00344804", title)

    def test_clean_function_expands_abbreviations(self):
        cleaned = _clean_job_title_for_headline("Banking Ops Excellence Analyst")
        self.assertEqual(cleaned, "Banking Operations Excellence Analyst")


if __name__ == "__main__":
    unittest.main()


class SummaryEvidenceTests(unittest.TestCase):
    """The current role's flagship quantified result must headline the summary."""

    @classmethod
    def setUpClass(cls):
        cls.profile = load_profile()

    def test_banking_job_summary_carries_current_role_aum_line(self):
        from jobhunter.resume.tailor import _transferable_summary

        job = _job("Banking Operations Excellence Analyst")
        job = Job(source="manual", title=job.title, company="Example Co",
                  url="https://example.test/job",
                  description=("banking operations, retail banking and wealth management "
                               "operations, process excellence, SOP, cross-functional"))
        summary = _transferable_summary(self.profile, job, [], "fallback summary")
        self.assertIn("150Cr", summary)
        self.assertIn("UHNI", summary)

    def test_unrelated_current_role_bullet_is_not_returned(self):
        from jobhunter.resume.tailor import _summary_evidence

        # A warehouse-ops JD with zero overlap with the current role's tags:
        # the flagship UHNI bullet must NOT be flagged for forced inclusion.
        job = Job(source="manual", title="Warehouse Operator", company="Example Co",
                  url="https://example.test/job",
                  description="Operate forklifts in cold storage; follow safety procedures and lift orders.")
        _, current = _summary_evidence(self.profile, job,
                                       ["Supply Chain & Manufacturing Operations"])
        self.assertIsNone(current)


class TitleSanityTests(unittest.TestCase):
    """Domain tokens must not leak and level-word stripping must not mangle roles."""

    @classmethod
    def setUpClass(cls):
        cls.profile = load_profile()

    def _target(self, title: str) -> str:
        return _safe_target_title(self.profile, _job(title))

    def test_portal_domain_is_stripped(self):
        cleaned = _clean_job_title_for_headline(
            "Senior Account Manager, Strategic Advisory Services - Job ID: 10521420 | Amazon.jobs")
        self.assertNotIn("Amazon.jobs", cleaned)
        self.assertNotIn("10521420", cleaned)

    def test_level_words_do_not_eat_the_role(self):
        title = self._target("Senior Account Manager, Strategic Advisory Services - Job ID: 10521420 | Amazon.jobs")
        self.assertNotIn("Amazon", title)
        self.assertNotEqual(title, "Cross-Functional Operations & Stakeholder Management")
        self.assertIn("Account", title)
        self.assertIn("Manager", title)


class CompetencyChipFloorTests(unittest.TestCase):
    """Core competencies: best JD-match first, never below 5 chips."""

    @classmethod
    def setUpClass(cls):
        cls.profile = load_profile()

    def test_paraphrased_llm_labels_floor_at_five_best_match_first(self):
        from jobhunter.resume.tailor import _ground_truth_competencies, _jd_matched_competency_names

        job = Job(source="manual", title="Operations Transformation Manager", company="Example Co",
                  url="https://example.test/job",
                  description=("process excellence, operating model transformation, continuous "
                               "improvement, cross-functional stakeholder management, data analytics"))
        out = _ground_truth_competencies(["Client Relationship Mgmt", "Paraphrased Label X"],
                                         self.profile, job)
        self.assertGreaterEqual(len(out), 5)
        names = {c.name for c in self.profile.competencies}
        self.assertGreaterEqual(len([c for c in out if c in names]), 3)
        matched = _jd_matched_competency_names(self.profile, job)
        if matched:
            self.assertEqual(out[0], matched[0])
