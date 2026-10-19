"""Compare saved experiment expectations with independently observed source facts."""
from omnibci.domain.signal_info import SignalInfo


def compare_source_expectations(binding: dict | None, info: SignalInfo) -> dict:
    parameters = (binding or {}).get("parameters", {})
    values = {
        "sample_rate": {"expected": parameters.get("sample_rate"), "actual": info.sample_rate},
        "channel_number": {"expected": parameters.get("channel_number"), "actual": info.output_row_count},
    }
    if info.provenance not in {"device-reported", "file-reported"}:
        status = "unverified"
    elif any(value["expected"] is not None and value["expected"] != value["actual"]
             for value in values.values()):
        status = "mismatched"
    elif any(value["expected"] is None for value in values.values()):
        status = "unconfirmed"
    else:
        status = "matched"
    return {"status": status, **values}


def validate_source_expectations(snapshot: dict, info: SignalInfo) -> None:
    binding = next((item for item in snapshot.get("PLUGINS.bindings", ())
                    if item.get("slot") == "acquisition.source"), None)
    comparison = compare_source_expectations(binding, info)
    if comparison["status"] == "mismatched":
        differences = []
        for key, label in (("sample_rate", "sample rate (Hz)"),
                           ("channel_number", "output rows (including event row)")):
            value = comparison[key]
            if value["expected"] is not None and value["expected"] != value["actual"]:
                differences.append(f"{label}: expected {value['expected']}, observed {value['actual']}")
        raise ValueError(
            "Source differs from the experiment profile: " + "; ".join(differences)
            + ". Adjust the source and read metadata again, or explicitly save and apply the observed values."
        )
