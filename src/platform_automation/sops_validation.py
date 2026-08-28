from typing import Any


def valid_age_recipients(document: Any) -> set[str]:
    if not isinstance(document, dict):
        return set()

    sops_metadata = document.get("sops")
    if not isinstance(sops_metadata, dict):
        return set()

    age_entries = sops_metadata.get("age")
    if not isinstance(age_entries, list):
        return set()

    return {
        entry["recipient"]
        for entry in age_entries
        if isinstance(entry, dict)
        and isinstance(entry.get("recipient"), str)
        and entry["recipient"].startswith("age1")
        and isinstance(entry.get("enc"), str)
        and bool(entry["enc"].strip())
    }


def validate_sops_document(
    document: Any,
    minimum_age_recipients: int = 1,
) -> list[str]:
    if (
        isinstance(minimum_age_recipients, bool)
        or not isinstance(minimum_age_recipients, int)
        or minimum_age_recipients < 1
    ):
        raise ValueError("minimum_age_recipients must be a positive integer")

    if not isinstance(document, dict):
        return ["secrets file must contain a YAML object"]

    sops_metadata = document.get("sops")

    if not isinstance(sops_metadata, dict):
        return ["secrets file is not SOPS-encrypted"]

    errors: list[str] = []
    recipients = valid_age_recipients(document)

    if not recipients:
        errors.append("SOPS metadata has no age recipient")
    elif len(recipients) < minimum_age_recipients:
        errors.append(
            "SOPS metadata requires at least "
            f"{minimum_age_recipients} unique age recipients; "
            f"found {len(recipients)}"
        )

    mac = sops_metadata.get("mac")

    if not isinstance(mac, str) or not mac.startswith("ENC["):
        errors.append("SOPS metadata has no encrypted MAC")

    version = sops_metadata.get("version")

    if not isinstance(version, str) or not version:
        errors.append("SOPS metadata has no version")

    return errors
