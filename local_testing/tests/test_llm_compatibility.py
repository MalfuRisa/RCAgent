"""Check actual HTTP payloads and parsed results without a model service."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import httpx
from openai import BadRequestError
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.chat_model import RCAChatOpenAI
from config import settings
from local_testing.check_llm_compatibility import PROBES
from models.schemas import RCAAgentExplaination, SymptomList


class StructuredOutputTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()

    def model(self, result, *, method="function_calling", error=False, prose=False):
        def handler(request):
            payload = json.loads(request.content)
            self.requests.append(payload)
            if error:
                return httpx.Response(400, json={"error": {
                    "message": "This response_format type is unavailable now",
                    "type": "invalid_request_error", "code": "invalid_parameter_error",
                }})
            message = {"role": "assistant", "content": json.dumps(result)}
            finish = "stop"
            if "tools" in payload and not prose:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_test", "type": "function", "function": {
                        "name": payload["tools"][0]["function"]["name"],
                        "arguments": json.dumps(result),
                    },
                }]}
                finish = "tool_calls"
            return httpx.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 1,
                "model": "compatibility-test-model",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            })

        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        return RCAChatOpenAI(
            model="compatibility-test-model", api_key="not-a-real-key",
            base_url="https://model.invalid/v1", http_client=client,
            structured_output_method=method, max_retries=0,
        )

    def test_all_four_schemas_use_tools_and_parse_values(self):
        for stage, schema, expected in PROBES:
            with self.subTest(stage=stage):
                result = self.model(expected).with_structured_output(schema).invoke("submit sample")
                self.assertEqual(result.model_dump(), schema.model_validate(expected).model_dump())
                payload = self.requests[-1]
                self.assertNotIn("response_format", payload)
                self.assertEqual(payload["tool_choice"]["function"]["name"], schema.__name__)

    def test_explicit_json_schema_configuration(self):
        for stage, schema, expected in PROBES:
            with self.subTest(stage=stage):
                result = self.model(expected, method="json_schema").with_structured_output(schema).invoke("submit")
                self.assertEqual(result.model_dump(), schema.model_validate(expected).model_dump())
                self.assertEqual(self.requests[-1]["response_format"]["type"], "json_schema")
                self.assertNotIn("tools", self.requests[-1])

    def test_call_site_can_explicitly_override_method(self):
        self.model({"symptoms": []}).with_structured_output(
            SymptomList, method="json_schema"
        ).invoke("submit")
        self.assertEqual(self.requests[-1]["response_format"]["type"], "json_schema")

    def test_missing_function_submission_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "did not submit"):
            self.model({"symptoms": []}, prose=True).with_structured_output(SymptomList).invoke("submit")

    def test_invalid_fields_are_not_accepted_as_diagnosis(self):
        with self.assertRaises(ValidationError):
            self.model({"steps": 42}).with_structured_output(RCAAgentExplaination).invoke("submit")

    def test_include_raw_retains_explicit_parser_error_contract(self):
        result = self.model({"steps": 42}).with_structured_output(
            RCAAgentExplaination, include_raw=True
        ).invoke("submit")
        self.assertIsNone(result["parsed"])
        self.assertIsInstance(result["parsing_error"], ValidationError)

    def test_interface_error_propagates_without_fallback(self):
        with self.assertRaises(BadRequestError):
            self.model({}, error=True, method="json_schema").with_structured_output(SymptomList).invoke("submit")
        self.assertEqual(len(self.requests), 1)

    def test_regular_chat_and_query_tool_binding_still_work(self):
        model = self.model({"answer": "ok"})
        model.invoke("hello")
        self.assertNotIn("tools", self.requests[-1])
        self.assertNotIn("response_format", self.requests[-1])
        model.bind_tools([{"type": "function", "function": {
            "name": "read_sample", "description": "synthetic query",
            "parameters": {"type": "object", "properties": {}},
        }}]).invoke("query")
        self.assertEqual(self.requests[-1]["tools"][0]["function"]["name"], "read_sample")
        self.assertNotIn("structured_output_method", self.requests[-1])

    def test_bad_method_is_rejected_before_any_request(self):
        with self.assertRaises(ValueError):
            self.model({}).with_structured_output(SymptomList, method="json_mode")
        self.assertEqual(self.requests, [])

    def test_config_factory_and_advanced_override(self):
        with mock.patch.multiple(settings, LLM_API_KEY="not-a-real-key", LLM_BASE_URL="https://model.invalid/v1",
                                 LLM_STRUCTURED_OUTPUT_METHOD="function_calling",
                                 LLM_ADVANCED_STRUCTURED_OUTPUT_METHOD="json_schema", _CHAT_MODELS={}):
            self.assertIsInstance(settings.get_chat_model("mini"), RCAChatOpenAI)
            self.assertEqual(settings.get_chat_model("mini").structured_output_method, "function_calling")
            self.assertEqual(settings.get_chat_model("advanced").structured_output_method, "json_schema")
            with self.assertRaises(ValueError):
                settings._build_chat_model("test", structured_output_method="unknown")


if __name__ == "__main__":
    unittest.main()
