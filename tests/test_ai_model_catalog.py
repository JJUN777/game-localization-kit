from __future__ import annotations

import unittest

from glk.application.ai_model_catalog import load_gemini_model_catalog
from glk.infrastructure.gemini_layout import DEFAULT_MODEL


class GeminiModelCatalogTests(unittest.TestCase):
    def test_catalog_has_unique_api_model_ids_and_default(self) -> None:
        catalog = load_gemini_model_catalog()
        model_ids = [model["id"] for model in catalog["models"]]

        self.assertEqual(catalog["provider"], "gemini")
        self.assertEqual(
            catalog["source_url"],
            "https://ai.google.dev/gemini-api/docs/models",
        )
        self.assertEqual(len(model_ids), len(set(model_ids)))
        self.assertIn(DEFAULT_MODEL, model_ids)
        self.assertEqual(
            model_ids,
            [
                "gemini-3.5-flash",
                "gemini-3.1-flash-lite",
                "gemini-2.5-flash",
                "gemini-2.5-pro",
                "gemini-2.5-flash-lite",
            ],
        )
        self.assertEqual(
            sum(model["recommended"] for model in catalog["models"]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
