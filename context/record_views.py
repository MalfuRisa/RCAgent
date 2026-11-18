"""Lossless views for historical report readers, independent of role/domain policies."""


def review_payload(binding, snapshot):
    limitations = []
    observations = []
    for original in snapshot["observation"]:
        if original.get("run_id") != binding.run_id:
            continue
        row = {key: value for key, value in original.items() if key != "limitations"}
        refs = []
        for text in original.get("limitations", []):
            if text not in limitations:
                limitations.append(text)
            refs.append(limitations.index(text))
        row["limitation_refs"] = refs
        observations.append(row)
    return {
        "observations": observations,
        "judgments": [row for row in snapshot["judgment"] if row.get("run_id") == binding.run_id],
        "shared_limitations": limitations,
    }
