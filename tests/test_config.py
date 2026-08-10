import os
import unittest
from unittest.mock import patch

from evoagent.config import Settings


class SettingsTests(unittest.TestCase):
    def test_resolves_siliconflow_defaults(self):
        with patch.dict(
            os.environ,
            {
                "EVOAGENT_LLM_PROVIDER": "siliconflow",
                "EVOAGENT_SILICONFLOW_API_KEY": "test-key",
                "EVOAGENT_LLM_BASE_URL": "",
                "EVOAGENT_LLM_MODEL": "",
            },
            clear=False,
        ):
            config = Settings.from_env().resolved_llm()

        self.assertEqual("siliconflow", config["provider"])
        self.assertEqual("https://api.siliconflow.cn/v1", config["base_url"])
        self.assertEqual("deepseek-ai/DeepSeek-V3", config["model"])
        self.assertEqual("test-key", config["api_key"])

    def test_siliconflow_requires_api_key(self):
        with patch.dict(
            os.environ,
            {
                "EVOAGENT_LLM_PROVIDER": "siliconflow",
                "EVOAGENT_SILICONFLOW_API_KEY": "",
                "EVOAGENT_LLM_API_KEY": "",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "SiliconFlow requires"):
                Settings.from_env().resolved_llm()


if __name__ == "__main__":
    unittest.main()
