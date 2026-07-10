from __future__ import annotations

import types
import unittest
from unittest.mock import patch

import analytics


class _FakePreview:
    def to_string(self, index=False):
        return "name  quantity\nWidget 5\nGadget 2"


class _FakeDataFrame:
    columns = ["name", "quantity"]

    def __len__(self):
        return 2

    def head(self, n):
        return _FakePreview()

    def __str__(self):
        return "<fake dataframe>"

    def __getitem__(self, key):
        if key == "name":
            return types.SimpleNamespace(tolist=lambda: ["Widget", "Gadget"], values=["Widget", "Gadget"])
        if key == "quantity":
            return types.SimpleNamespace(tolist=lambda: [5, 2], values=[5, 2])
        raise KeyError(key)


class _RecordingClient:
    def __init__(self, response="ok"):
        self.response = response
        self.prompts = []
        self.model_name = "fake-model"

    def generate(self, prompt):
        self.prompts.append(prompt)
        return self.response


class AnalyticsTests(unittest.TestCase):
    def test_default_model_is_current_flash(self):
        self.assertNotIn("1.5", analytics.DEFAULT_MODEL_NAME)
        self.assertEqual(analytics.DEFAULT_MODEL_NAME, "gemini-3.5-flash")

    def test_public_functions_use_client_boundary(self):
        df = _FakeDataFrame()
        client = _RecordingClient()

        with patch.object(analytics, "_get_client", return_value=client):
            self.assertEqual(analytics.generate_insights(df, client=client).text, "ok")
            self.assertEqual(analytics.predict_stock_needs(df, client=client).text, "ok")
            self.assertEqual(
                analytics.categorize_product(df, "Widget", "Useful widget", client=client).text,
                "ok",
            )
            self.assertEqual(analytics.generate_report(df, client=client).text, "ok")

        self.assertEqual(len(client.prompts), 4)
        self.assertTrue(all("Inventory context:" in prompt for prompt in client.prompts))
        self.assertTrue(any("Columns: name, quantity" in prompt for prompt in client.prompts))
        self.assertTrue(any("Widget" in prompt for prompt in client.prompts))

    def test_falls_back_without_api_key(self):
        df = _FakeDataFrame()
        with patch.object(analytics, "_api_key_available", return_value=False):
            result = analytics.generate_insights(df)

        self.assertEqual(result.status, "fallback")
        self.assertIn("Deterministic inventory insights summary", result.text)
        self.assertIn("AI analysis was unavailable", result.text)
        self.assertIn("Products: 2", result.text)

    def test_falls_back_when_client_raises(self):
        class BoomClient:
            model_name = "fake-model"

            def generate(self, prompt):
                raise RuntimeError("model unavailable")

        result = analytics.generate_insights(_FakeDataFrame(), client=BoomClient())
        self.assertEqual(result.status, "fallback")
        self.assertIn("model unavailable", result.error or "")
        self.assertIn("Deterministic inventory insights summary", result.text)

    def test_gemini_client_rejects_unsupported_sdk_surface(self):
        fake_genai = types.SimpleNamespace(configure=lambda *args, **kwargs: None)

        with self.assertRaisesRegex(
            RuntimeError,
            "must expose configure\\(\\) and GenerativeModel",
        ):
            analytics.GeminiAnalyticsClient(genai_module=fake_genai)

    def test_gemini_client_uses_generative_model(self):
        created_models = []

        class FakeResponse:
            text = "generated text"

        class FakeModel:
            def __init__(self, model_name):
                created_models.append(model_name)

            def generate_content(self, prompt):
                self.last_prompt = prompt
                return FakeResponse()

        fake_genai = types.SimpleNamespace(
            configure=lambda *args, **kwargs: None,
            GenerativeModel=FakeModel,
        )

        client = analytics.GeminiAnalyticsClient(
            model_name="fake-model",
            genai_module=fake_genai,
        )

        self.assertEqual(client.generate("hello"), "generated text")
        self.assertEqual(created_models, ["fake-model"])


if __name__ == "__main__":
    unittest.main()
