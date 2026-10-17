from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from career_agent import runtime


class RuntimeSecretTests(unittest.TestCase):
    def test_integration_keys_reload_and_removed_values_do_not_linger(self):
        with patch.dict(
            os.environ,
            {"BRAVE_API_KEY": "old", "ADZUNA_APP_ID": "old-id", "ADZUNA_APP_KEY": "old-key"},
            clear=False,
        ), patch(
            "career_agent.runtime.secrets",
            side_effect=[
                {"BRAVE_API_KEY": "new", "ADZUNA_APP_ID": "new-id", "ADZUNA_APP_KEY": "new-key"},
                {"BRAVE_API_KEY": "rotated"},
            ],
        ):
            runtime._load_integration_environment()
            self.assertEqual("new", os.environ["BRAVE_API_KEY"])
            self.assertEqual("new-id", os.environ["ADZUNA_APP_ID"])

            runtime._load_integration_environment()
            self.assertEqual("rotated", os.environ["BRAVE_API_KEY"])
            self.assertNotIn("ADZUNA_APP_ID", os.environ)
            self.assertNotIn("ADZUNA_APP_KEY", os.environ)


if __name__ == "__main__":
    unittest.main()
