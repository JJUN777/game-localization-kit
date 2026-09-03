from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from glk.infrastructure.ai_provider import (
    create_glossary_triage_provider,
    create_pdf_icon_audit_provider,
    create_translation_provider,
    resolve_ai_model_name,
    resolve_ai_provider_name,
)


class AiProviderTests(unittest.TestCase):
    def test_gemini_remains_the_default_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {"GLK_AI_PROVIDER": ""},
        ):
            self.assertEqual(resolve_ai_provider_name(directory), "gemini")

    def test_openai_provider_and_model_are_loaded_from_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                'GLK_AI_PROVIDER="openai"\n'
                'OPENAI_API_KEY="sk-test"\n'
                'OPENAI_MODEL="gpt-test"\n',
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"GLK_AI_PROVIDER": "", "OPENAI_MODEL": ""},
            ):
                self.assertEqual(resolve_ai_provider_name(root), "openai")
                self.assertEqual(resolve_ai_model_name(settings_root=root), "gpt-test")
                with patch(
                    "glk.infrastructure.ai_provider."
                    "OpenAITranslationProvider.from_environment",
                    return_value="openai-provider",
                ) as factory:
                    provider = create_translation_provider(settings_root=root)

                with patch(
                    "glk.infrastructure.ai_provider."
                    "OpenAIPdfIconAuditProvider.from_environment",
                    return_value="openai-icon-provider",
                ) as icon_factory:
                    icon_provider = create_pdf_icon_audit_provider(
                        settings_root=root
                    )

                with patch(
                    "glk.infrastructure.ai_provider."
                    "OpenAIGlossaryTriageProvider.from_environment",
                    return_value="openai-glossary-provider",
                ) as glossary_factory:
                    glossary_provider = create_glossary_triage_provider(
                        settings_root=root
                    )

            self.assertEqual(provider, "openai-provider")
            factory.assert_called_once_with(None, settings_root=root)
            self.assertEqual(icon_provider, "openai-icon-provider")
            icon_factory.assert_called_once_with(None, settings_root=root)
            self.assertEqual(glossary_provider, "openai-glossary-provider")
            glossary_factory.assert_called_once_with(None, settings_root=root)


if __name__ == "__main__":
    unittest.main()
