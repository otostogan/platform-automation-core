import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "examples" / "consumer" / "application" / "app" / "app.py"


def load_example_app():
    specification = importlib.util.spec_from_file_location(
        "example_app",
        APP_PATH,
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class ExampleAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = load_example_app()

    def test_health_endpoint(self) -> None:
        status, body = self.application.response_for_path("/healthz")

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"status": "ok"},
        )

    def test_version_endpoint(self) -> None:
        status, body = self.application.response_for_path("/version")

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"version": "0.1.0"},
        )

    def test_root_endpoint(self) -> None:
        status, body = self.application.response_for_path("/")

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["application"],
            "platform-example",
        )

    def test_unknown_endpoint(self) -> None:
        status, body = self.application.response_for_path("/missing")

        self.assertEqual(status, 404)
        self.assertEqual(
            json.loads(body),
            {"error": "not found"},
        )


if __name__ == "__main__":
    unittest.main()
