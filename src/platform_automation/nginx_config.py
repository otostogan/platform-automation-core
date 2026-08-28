from typing import Any


MANAGED_HEADER = "# Managed by platform. Do not edit manually."


class NginxConfigError(ValueError):
    pass


def normalize_raw_snippet(value: Any) -> str:
    if value is None:
        return ""

    if not isinstance(value, str):
        raise NginxConfigError("Raw nginx snippet must be a string or null")

    return value.strip()


def render_fragment(lines: list[str]) -> str:
    return "\n".join([MANAGED_HEADER, *lines]) + "\n"


def generate_vhost_fragments(
    manifest: dict[str, Any],
    allowed_raw_projects: set[str] = None,
) -> dict[str, str]:
    if allowed_raw_projects is None:
        allowed_raw_projects = set()

    project = manifest["project"]
    fragments: dict[str, str] = {}

    for domain in manifest["domains"]:
        host = domain["host"]
        nginx = domain["nginx"]

        if host in fragments:
            raise NginxConfigError(f"Duplicate domain host: {host}")

        vhost_lines: list[str] = []

        for directive in (
            "client_max_body_size",
            "proxy_connect_timeout",
            "proxy_read_timeout",
            "proxy_send_timeout",
        ):
            value = nginx.get(directive)

            if value is not None:
                vhost_lines.append(f"{directive} {value};")

        gzip = nginx.get("gzip")

        if gzip is not None:
            if gzip["enabled"]:
                gzip_types = gzip["types"]

                if not gzip_types:
                    raise NginxConfigError(
                        f"gzip.types cannot be empty when gzip is enabled for {host}"
                    )

                vhost_lines.extend(
                    [
                        "gzip on;",
                        "gzip_vary on;",
                        f"gzip_types {' '.join(gzip_types)};",
                    ]
                )
            else:
                vhost_lines.append("gzip off;")

        raw_vhost = normalize_raw_snippet(nginx.get("raw_vhost_snippet"))
        raw_location = normalize_raw_snippet(nginx.get("raw_location_snippet"))

        if (raw_vhost or raw_location) and project not in allowed_raw_projects:
            raise NginxConfigError(
                f"Raw nginx snippets are not allowed for project: {project}"
            )

        if raw_vhost:
            vhost_lines.extend(
                [
                    "",
                    f"# Raw vhost snippet for allowlisted project: {project}",
                    raw_vhost,
                ]
            )

        fragments[host] = render_fragment(vhost_lines)

        if raw_location:
            fragments[f"{host}_location"] = render_fragment(
                [
                    f"# Raw location snippet for allowlisted project: {project}",
                    raw_location,
                ]
            )

    return fragments
