"""What an application ships for its own observability, as one bundle file.

A bundle is a fixed set of named files; a directory of dashboards is not one.
So the builder folds the directory into a single JSON document, the host
verifies that document like every other bundle file, and the fixed set grows
by exactly one optional name.

The rules here are the ones that keep one application's dashboards from
breaking another's, or the platform's own:

- a dashboard may read only the two data sources the platform provisions;
- its ``uid`` is the platform's to choose, so two applications cannot collide
  and none can replace a platform dashboard;
- it is never editable in the UI — what is shipped is what is shown.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional

API_VERSION = "platform-observability/v1"
BUNDLE_PATH = "platform-observability.json"

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_DASHBOARDS = 20
MAX_DASHBOARD_BYTES = 512 * 1024
# All of them together are one bundle file, and the host refuses a bundle
# file over five megabytes. The build must not make what no host will take.
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
# Grafana refuses a uid longer than this.
MAX_UID_LENGTH = 40

PLATFORM_SOURCES = frozenset({"platform-prometheus", "platform-loki"})
# Grafana's own pseudo-sources: annotations, expressions, "mixed" panels and
# a panel that reuses another panel's result.
BUILT_IN_SOURCES = frozenset(
    {"-- Grafana --", "grafana", "-- Mixed --", "-- Dashboard --", "__expr__"}
)
ALLOWED_SOURCES = PLATFORM_SOURCES | BUILT_IN_SOURCES


class ObservabilityBundleError(ValueError):
    pass


def declared_directory(manifest: dict[str, Any]) -> Optional[str]:
    block = manifest.get("observability")
    return block.get("dashboards") if isinstance(block, dict) else None


def dashboard_uid(project: str, environment: str, name: str) -> str:
    """Readable when it fits, and unique either way."""
    readable = f"{project}-{environment}-{name}"
    if len(readable) <= MAX_UID_LENGTH:
        return readable
    digest = hashlib.sha256(readable.encode("utf-8")).hexdigest()[:12]
    return f"{readable[: MAX_UID_LENGTH - 13]}-{digest}"


def _sources(node: Any, path: str = "$") -> list:
    """Every ``datasource`` reference in the dashboard, with where it is."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}"
            if key == "datasource":
                found.append((here, value))
            else:
                found += _sources(value, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _sources(value, f"{path}[{index}]")
    return found


def _source_error(where: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    uid = value.get("uid") if isinstance(value, dict) else value
    if uid is None and isinstance(value, dict):
        # {"type": "prometheus"} alone means "the default source" to Grafana
        return f"{where}: name the data source by uid"
    if not isinstance(uid, str):
        return f"{where}: data source must be a uid"
    if uid in ALLOWED_SOURCES:
        return None
    if uid.startswith("$"):
        return (
            f"{where}: {uid} is a placeholder — a dashboard names its data source"
            " directly: " + " or ".join(sorted(PLATFORM_SOURCES))
        )
    return (
        f"{where}: unknown data source {uid!r}; the platform provisions "
        + " and ".join(sorted(PLATFORM_SOURCES))
    )


def dashboard_errors(name: str, dashboard: Any) -> list:
    """Why this dashboard cannot be shipped; empty when it can."""
    prefix = f"dashboard {name}"
    if not NAME_PATTERN.fullmatch(name):
        return [
            f"{prefix}: the file name must be lowercase letters, digits and dashes,"
            " up to 63 characters"
        ]
    if not isinstance(dashboard, dict):
        return [f"{prefix}: must be a JSON object"]
    errors = []
    title = dashboard.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append(f"{prefix}: needs a title")
    if not isinstance(dashboard.get("panels"), list):
        errors.append(f"{prefix}: needs a list of panels")
    if "__inputs" in dashboard or "__requires" in dashboard:
        errors.append(
            f'{prefix}: exported "for sharing externally" — its data sources are'
            " placeholders. Export it without that option"
        )
    size = len(json.dumps(dashboard, ensure_ascii=False).encode("utf-8"))
    if size > MAX_DASHBOARD_BYTES:
        errors.append(f"{prefix}: {size} bytes; the limit is {MAX_DASHBOARD_BYTES}")
    for where, value in _sources(dashboard):
        problem = _source_error(where, value)
        if problem:
            errors.append(f"{prefix}: {problem}")
    return errors


def document_errors(document: Any) -> list:
    """Why this bundle file is not acceptable; empty when it is."""
    if not isinstance(document, dict):
        return ["observability document must be a JSON object"]
    if set(document) != {"api_version", "dashboards"}:
        return ["observability document has unexpected fields"]
    if document["api_version"] != API_VERSION:
        return [f"observability document must be {API_VERSION}"]
    dashboards = document["dashboards"]
    if not isinstance(dashboards, dict) or not dashboards:
        return ["observability document carries no dashboards"]
    if len(dashboards) > MAX_DASHBOARDS:
        return [f"at most {MAX_DASHBOARDS} dashboards per application"]
    errors = []
    for name in sorted(dashboards):
        errors += dashboard_errors(str(name), dashboards[name])
    return errors


def collect_document(app_root: Path, directory: str) -> bytes:
    """Fold ``directory`` of ``*.json`` dashboards into the bundle file."""
    root = app_root.resolve()
    source = root / directory
    if source.is_symlink() or not source.is_dir():
        raise ObservabilityBundleError(
            f"observability.dashboards is not a directory: {directory}"
        )
    try:
        source.resolve().relative_to(root)
    except ValueError as error:
        raise ObservabilityBundleError(
            f"observability.dashboards escapes the application root: {directory}"
        ) from error

    dashboards = {}
    for path in sorted(source.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise ObservabilityBundleError(
                f"{directory}/{path.name}: only regular *.json files belong here"
            )
        if path.stat().st_size > MAX_DASHBOARD_BYTES:
            raise ObservabilityBundleError(
                f"{directory}/{path.name}: larger than {MAX_DASHBOARD_BYTES} bytes"
            )
        try:
            dashboards[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ObservabilityBundleError(
                f"{directory}/{path.name}: not valid JSON"
            ) from error

    document = {"api_version": API_VERSION, "dashboards": dashboards}
    errors = document_errors(document)
    if errors:
        raise ObservabilityBundleError(
            "invalid application dashboards:\n" + "\n".join(errors)
        )
    content = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ObservabilityBundleError(
            f"{directory}: the dashboards together are {len(content)} bytes;"
            f" the limit for all of them is {MAX_DOCUMENT_BYTES}"
        )
    return content


def local_errors(app_root: Path, manifest: dict[str, Any]) -> list:
    """What the bundle build would say about the dashboards, without building.

    For the operator's terminal: ``doctor`` and the console's validation run
    this, so a dashboard that cannot be shipped is found before the deploy.
    """
    directory = declared_directory(manifest)
    if directory is None:
        return []
    try:
        collect_document(app_root, directory)
    except ObservabilityBundleError as error:
        return [line for line in str(error).splitlines() if line.strip()]
    return []


def count_dashboards(app_root: Path, manifest: dict[str, Any]) -> int:
    directory = declared_directory(manifest)
    if directory is None:
        return 0
    return len(json.loads(collect_document(app_root, directory))["dashboards"])


def load_document(content: bytes) -> dict:
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ObservabilityBundleError(
            "observability bundle file is not valid JSON"
        ) from error
    errors = document_errors(document)
    if errors:
        raise ObservabilityBundleError(
            "invalid application dashboards:\n" + "\n".join(errors)
        )
    return document


def provisioned(project: str, environment: str, name: str, dashboard: dict) -> dict:
    """The dashboard as Grafana gets it: the platform's uid, not editable."""
    prepared = dict(dashboard)
    prepared["uid"] = dashboard_uid(project, environment, name)
    # A numeric id belongs to the Grafana it was exported from.
    prepared["id"] = None
    prepared["editable"] = False
    tags = [tag for tag in prepared.get("tags") or [] if isinstance(tag, str)]
    for tag in (project, environment):
        if tag not in tags:
            tags.append(tag)
    prepared["tags"] = tags
    return prepared
