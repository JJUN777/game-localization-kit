from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from glk.config import resolve_settings_root
from glk.infrastructure.gemini_common import load_gemini_environment


class SettingsPathTests(unittest.TestCase):
    def test_explicit_root_precedes_environment_and_resolves_from_cwd(self) -> None:
        cwd = Path.cwd().resolve() / "work/current"

        resolved = resolve_settings_root(
            "explicit-settings",
            environment={
                "GLK_SETTINGS_ROOT": str(Path.cwd().resolve() / "ignored")
            },
            cwd=cwd,
            detect_editable_root=False,
        )

        self.assertEqual(resolved, cwd / "explicit-settings")

    def test_environment_root_is_independent_from_cwd(self) -> None:
        base = Path.cwd().resolve()
        configured = base / "shared/glk-settings"

        first = resolve_settings_root(
            environment={"GLK_SETTINGS_ROOT": str(configured)},
            cwd=base / "first",
            detect_editable_root=False,
        )
        second = resolve_settings_root(
            environment={"GLK_SETTINGS_ROOT": str(configured)},
            cwd=base / "second",
            detect_editable_root=False,
        )

        self.assertEqual(first, configured)
        self.assertEqual(second, configured)

    def test_editable_checkout_root_precedes_platform_user_directory(self) -> None:
        base = Path.cwd().resolve()
        checkout = base / "repos/game-localization-kit"

        resolved = resolve_settings_root(
            environment={},
            cwd=base / "unrelated",
            home=base / "users/test",
            platform="darwin",
            editable_root=checkout,
        )

        self.assertEqual(resolved, checkout)

    def test_installed_package_uses_platform_user_config_directory(self) -> None:
        base = Path.cwd().resolve()
        home = base / "users/test"

        self.assertEqual(
            resolve_settings_root(
                environment={},
                cwd=base / "unrelated",
                home=home,
                platform="darwin",
                detect_editable_root=False,
            ),
            home / "Library/Application Support/game-localization-kit",
        )
        self.assertEqual(
            resolve_settings_root(
                environment={"XDG_CONFIG_HOME": str(base / "xdg")},
                cwd=base / "unrelated",
                home=home,
                platform="linux",
                detect_editable_root=False,
            ),
            base / "xdg/game-localization-kit",
        )

    def test_provider_loads_only_the_resolved_settings_file(self) -> None:
        root = Path.cwd().resolve() / "stable/settings"
        with (
            patch(
                "glk.infrastructure.gemini_common.resolve_settings_root",
                return_value=root,
            ) as resolve,
            patch(
                "glk.infrastructure.gemini_common.dotenv_values",
                return_value={},
            ) as load,
        ):
            load_gemini_environment()

        resolve.assert_called_once_with(None)
        load.assert_called_once_with(root / ".env")

    def test_provider_settings_merge_without_mutating_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / ".env").write_text(
                'GEMINI_API_KEY="file-key"\n'
                'GEMINI_MODEL="file-model"\n',
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": " shell-key ",
                    "GEMINI_MODEL": "",
                },
                clear=True,
            ):
                settings = load_gemini_environment(root)
                environment_after_load = dict(os.environ)

        self.assertEqual(
            settings,
            {
                "GEMINI_API_KEY": "shell-key",
                "GEMINI_MODEL": "file-model",
            },
        )
        self.assertEqual(
            environment_after_load,
            {
                "GEMINI_API_KEY": " shell-key ",
                "GEMINI_MODEL": "",
            },
        )


if __name__ == "__main__":
    unittest.main()
