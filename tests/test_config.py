import importlib
import os
import sys
from types import ModuleType
from unittest import TestCase, mock


def _build_fake_dependencies():
    fake_dotenv = ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda: None

    fake_genai = ModuleType("google.generativeai")
    fake_genai.configure_calls = []

    def configure(**kwargs):
        fake_genai.configure_calls.append(kwargs)

    fake_genai.configure = configure

    fake_google = ModuleType("google")
    fake_google.__path__ = []
    fake_google.generativeai = fake_genai

    return fake_dotenv, fake_google, fake_genai


def _import_config(env):
    fake_dotenv, fake_google, fake_genai = _build_fake_dependencies()

    with mock.patch.dict(
        sys.modules,
        {
            "dotenv": fake_dotenv,
            "google": fake_google,
            "google.generativeai": fake_genai,
        },
        clear=False,
    ), mock.patch.dict(os.environ, env, clear=True):
        sys.modules.pop("config", None)
        module = importlib.import_module("config")
        pandasai_key_in_env = os.environ.get("PANDASAI_API_KEY")
        sys.modules.pop("config", None)

    return module, fake_genai, pandasai_key_in_env


class ConfigImportTests(TestCase):
    def test_import_without_api_keys_does_not_crash_or_write_none(self):
        module, fake_genai, pandasai_key_in_env = _import_config({})

        self.assertIsNone(module.GOOGLE_API_KEY)
        self.assertIsNone(module.PANDASAI_API_KEY)
        self.assertEqual(
            module.MISSING_CREDENTIALS,
            ["GOOGLE_API_KEY", "PANDASAI_API_KEY"],
        )
        self.assertEqual(fake_genai.configure_calls, [])
        self.assertIsNone(pandasai_key_in_env)

    def test_import_with_api_keys_configures_and_persists_only_strings(self):
        module, fake_genai, pandasai_key_in_env = _import_config(
            {
                "GOOGLE_API_KEY": "google-key",
                "PANDASAI_API_KEY": "pandasai-key",
            }
        )

        self.assertEqual(module.GOOGLE_API_KEY, "google-key")
        self.assertEqual(module.PANDASAI_API_KEY, "pandasai-key")
        self.assertEqual(module.MISSING_CREDENTIALS, [])
        self.assertEqual(fake_genai.configure_calls, [{"api_key": "google-key"}])
        self.assertEqual(pandasai_key_in_env, "pandasai-key")

    def test_default_gemini_model_is_current_and_overridable(self):
        module, _, _ = _import_config({})
        self.assertEqual(module.DEFAULT_GEMINI_MODEL, "gemini-3.5-flash")

        module, _, _ = _import_config({"GEMINI_MODEL_NAME": "gemini-custom-test"})
        self.assertEqual(module.DEFAULT_GEMINI_MODEL, "gemini-custom-test")
