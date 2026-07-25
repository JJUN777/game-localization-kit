from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from glk.application.ai_settings_service import (
    AiSettingsError,
    AiSettingsService,
)
from glk.infrastructure.gemini_common import DEFAULT_MODEL


class AiSettingsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.settings_root = Path(self.temporary_directory.name)
        self.environment_patch = patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "", "GEMINI_MODEL": ""},
        )
        self.environment_patch.start()

    def tearDown(self) -> None:
        self.environment_patch.stop()
        self.temporary_directory.cleanup()

    def test_reports_default_without_exposing_a_key(self) -> None:
        status = AiSettingsService(self.settings_root).status()

        self.assertFalse(status.api_key_configured)
        self.assertEqual(status.api_key_source, "missing")
        self.assertEqual(status.model, DEFAULT_MODEL)
        self.assertEqual(status.model_source, "default")
        self.assertNotIn("api_key", status.to_dict())

    def test_saves_settings_atomically_and_restricts_permissions(self) -> None:
        service = AiSettingsService(self.settings_root)
        status = service.save(
            api_key="test-secret-key",
            model="gemini-2.5-pro",
        )

        env_path = self.settings_root / ".env"
        text = env_path.read_text(encoding="utf-8")
        self.assertIn('GEMINI_API_KEY="test-secret-key"', text)
        self.assertIn('GEMINI_MODEL="gemini-2.5-pro"', text)
        self.assertTrue(status.api_key_configured)
        self.assertEqual(status.api_key_source, "env_file")
        self.assertEqual(status.model, "gemini-2.5-pro")
        self.assertNotIn("test-secret-key", repr(status.to_dict()))
        self.assertEqual(os.environ["GEMINI_API_KEY"], "")
        self.assertEqual(os.environ["GEMINI_MODEL"], "")
        if os.name != "nt":
            mode = stat.S_IMODE(env_path.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_preserves_unrelated_lines_and_blank_key_keeps_current_key(
        self,
    ) -> None:
        env_path = self.settings_root / ".env"
        env_path.write_text(
            "# local settings\n"
            "UNRELATED=value\n"
            'GEMINI_API_KEY="existing-key"\n'
            "GEMINI_MODEL=old-model\n"
            "GEMINI_MODEL=duplicate-model\n",
            encoding="utf-8",
        )
        service = AiSettingsService(self.settings_root)

        service.save(api_key="", model="custom/model-v2")

        text = env_path.read_text(encoding="utf-8")
        self.assertIn("# local settings", text)
        self.assertIn("UNRELATED=value", text)
        self.assertIn('GEMINI_API_KEY="existing-key"', text)
        self.assertEqual(text.count("GEMINI_MODEL="), 1)
        self.assertIn('GEMINI_MODEL="custom/model-v2"', text)

    def test_environment_values_remain_effective_after_file_save(self) -> None:
        service = AiSettingsService(
            self.settings_root,
            environment={
                "GEMINI_API_KEY": "shell-key",
                "GEMINI_MODEL": "shell-model",
            },
        )

        status = service.save(
            api_key="file-key",
            model="file-model",
        )

        self.assertEqual(status.api_key_source, "environment")
        self.assertEqual(status.model, "shell-model")
        self.assertEqual(status.model_source, "environment")
        self.assertEqual(
            status.environment_override,
            {"api_key": True, "model": True},
        )
        text = (self.settings_root / ".env").read_text(encoding="utf-8")
        self.assertIn('GEMINI_API_KEY="file-key"', text)
        self.assertIn('GEMINI_MODEL="file-model"', text)

    def test_rejects_invalid_values_without_changing_the_file(self) -> None:
        env_path = self.settings_root / ".env"
        env_path.write_text("UNRELATED=value\n", encoding="utf-8")
        service = AiSettingsService(self.settings_root)

        with self.assertRaises(AiSettingsError):
            service.save(api_key="bad key", model="gemini-2.5-flash")
        self.assertEqual(
            env_path.read_text(encoding="utf-8"),
            "UNRELATED=value\n",
        )

        with self.assertRaises(AiSettingsError):
            service.save(api_key="", model="../invalid model")
        self.assertEqual(
            env_path.read_text(encoding="utf-8"),
            "UNRELATED=value\n",
        )


if __name__ == "__main__":
    unittest.main()
