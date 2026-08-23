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
