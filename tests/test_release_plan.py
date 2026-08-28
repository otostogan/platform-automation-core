import re
import tempfile
import unittest
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from scripts.release_plan import (
    build_release_plan,
    write_github_environment,
)
from platform_automation import __version__


class ReleasePlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository_root = Path(self.temporary_directory.name)
        (self.repository_root / "docs/releases").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_release_notes(self, tag: str) -> None:
        (self.repository_root / f"docs/releases/{tag}.md").write_text(
            f"# Release {tag}\n",
            encoding="utf-8",
        )

    def test_builds_all_paths_from_v020(self) -> None:
        self.write_release_notes("v0.2.0")

        plan = build_release_plan(
            tag="v0.2.0",
            runtime_version="0.2.0",
            collection_version="0.2.0",
            repository_root=self.repository_root,
        )

        self.assertEqual(plan.version, "0.2.0")
        self.assertEqual(plan.tag, "v0.2.0")
        self.assertEqual(
            plan.runtime_artifact,
            "platform_automation_runtime-0.2.0-py3-none-any.whl",
        )
        self.assertEqual(plan.collection_artifact, "otostogan-platform-0.2.0.tar.gz")
        self.assertEqual(plan.release_notes, "docs/releases/v0.2.0.md")

    def test_package_version_has_one_source(self) -> None:
        repository_root = Path(__file__).parents[1]
        configuration = tomllib.loads(
            (repository_root / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertNotIn("version", configuration["project"])
        self.assertIn("version", configuration["project"]["dynamic"])
        self.assertEqual(
            configuration["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "platform_automation.__version__"},
        )

    def test_rejects_tag_mismatch(self) -> None:
        self.write_release_notes("v0.2.0")

        with self.assertRaisesRegex(ValueError, "does not match runtime"):
            build_release_plan(
                tag="v0.3.0",
                runtime_version="0.2.0",
                collection_version="0.2.0",
                repository_root=self.repository_root,
            )

    def test_rejects_collection_version_mismatch(self) -> None:
        self.write_release_notes("v0.2.0")

        with self.assertRaisesRegex(ValueError, "collection version"):
            build_release_plan(
                tag="v0.2.0",
                runtime_version="0.2.0",
                collection_version="0.1.0",
                repository_root=self.repository_root,
            )

    def test_rejects_non_stable_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "is not semantic"):
            build_release_plan(
                tag="v0.2.0-rc.1",
                runtime_version="0.2.0-rc.1",
                collection_version="0.2.0-rc.1",
                repository_root=self.repository_root,
            )

    def test_rejects_missing_release_notes(self) -> None:
        with self.assertRaisesRegex(ValueError, "release notes do not exist"):
            build_release_plan(
                tag="v0.2.0",
                runtime_version="0.2.0",
                collection_version="0.2.0",
                repository_root=self.repository_root,
            )

    def test_exports_github_environment(self) -> None:
        self.write_release_notes("v0.2.0")
        plan = build_release_plan(
            tag="v0.2.0",
            runtime_version="0.2.0",
            collection_version="0.2.0",
            repository_root=self.repository_root,
        )
        destination = self.repository_root / "github.env"

        write_github_environment(plan, destination)

        self.assertEqual(
            destination.read_text(encoding="utf-8").splitlines(),
            ["RELEASE_VERSION=0.2.0"],
        )

    def test_release_workflow_uses_versioned_artifact_paths(self) -> None:
        workflow = (
            Path(__file__).parents[1] / ".github/workflows/release.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "dist/platform_automation_runtime-${RELEASE_VERSION}-py3-none-any.whl",
            workflow,
        )
        self.assertIn(
            "dist/otostogan-platform-${RELEASE_VERSION}.tar.gz",
            workflow,
        )
        self.assertIn(
            '--notes-file "docs/releases/v${RELEASE_VERSION}.md"',
            workflow,
        )

    def test_all_core_consumer_pins_match_current_release(self) -> None:
        repository_root = Path(__file__).parents[1]
        expected_tag = f"v{__version__}"
        semantic_tag = r"(v[0-9]+\.[0-9]+\.[0-9]+)"
        tag_patterns = (
            re.compile(r"otostogan/platform-automation-core[^\s\"'`]*@" + semantic_tag),
            re.compile(r"\bcore-version:\s*" + semantic_tag),
            re.compile(r"\bgh release download\s+" + semantic_tag),
            re.compile(r"\bdefault:\s*" + semantic_tag),
            re.compile(r"\bpin\s+`" + semantic_tag + r"`"),
        )
        collection_pattern = re.compile(
            r"otostogan-platform-([0-9]+\.[0-9]+\.[0-9]+)\.tar\.gz"
        )
        tag_pins = []
        collection_pins = []
        text_suffixes = {".json", ".md", ".py", ".toml", ".yaml", ".yml"}

        for root_name in (".github", "examples"):
            for path in (repository_root / root_name).rglob("*"):
                if not path.is_file() or path.suffix not in text_suffixes:
                    continue
                relative_path = path.relative_to(repository_root).as_posix()
                content = path.read_text(encoding="utf-8")
                for pattern in tag_patterns:
                    tag_pins.extend(
                        (relative_path, match) for match in pattern.findall(content)
                    )
                collection_pins.extend(
                    (relative_path, match)
                    for match in collection_pattern.findall(content)
                )

        expected_sources = {
            ".github/actions/build-bundle/action.yml",
            ".github/workflows/reusable-deploy.yml",
            "examples/consumer/README.md",
            "examples/consumer/application/.github/workflows/deploy.yml",
            "examples/consumer/company-infra/.github/workflows/converge.yml",
        }
        discovered_sources = {path for path, _ in [*tag_pins, *collection_pins]}
        invalid_pins = [
            f"{path}: {value}" for path, value in tag_pins if value != expected_tag
        ]
        invalid_pins.extend(
            f"{path}: {value}"
            for path, value in collection_pins
            if value != __version__
        )

        self.assertTrue(
            expected_sources.issubset(discovered_sources),
            f"core release pins were not discovered in: "
            f"{sorted(expected_sources - discovered_sources)}",
        )
        self.assertFalse(
            invalid_pins,
            "core release pins do not match "
            f"{expected_tag}: {', '.join(invalid_pins)}",
        )


if __name__ == "__main__":
    unittest.main()
