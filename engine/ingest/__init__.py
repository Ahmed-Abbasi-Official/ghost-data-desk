"""Phase 0: parse every evidence file once into SCHEMA.md observations (records + coverage)."""
import importlib
import os
import sys

from common import DATASET, read_csv


def _module_for(path):
    name = os.path.basename(path)
    if path.endswith(".sql"):
        return "sql_snapshots"
    if path.endswith(".log"):
        return "logs"
    if path.endswith(".pdf"):
        return "pdf_docs"
    if path.endswith(".png"):
        return "images"
    if path.endswith(".txt"):
        return "text_records"
    if name.startswith(("customer_data_snapshot_bundle", "service_resource_export")):
        return "json_bundles"
    if path.endswith(".json"):
        return "json_misc"
    return None


def build_index(verbose=False):
    """Return (observations, manifest) where manifest maps file_id -> manifest row."""
    manifest = {row["file_id"]: row for row in read_csv("evidence_files.csv")}
    observations, missing = [], set()
    for file_id in sorted(manifest, key=lambda f: int(f.rsplit("_", 1)[1])):
        row = manifest[file_id]
        path = os.path.join(DATASET, row["file_path"])
        mod_name = _module_for(path)
        try:
            module = importlib.import_module("ingest." + mod_name)
        except ImportError:
            missing.add(mod_name)
            continue
        try:
            items = module.parse(path, file_id, row["captured_at"])
        except Exception as exc:  # one bad file must not stop the audit
            print("WARN parse failed %s (%s): %s" % (file_id, row["file_path"], exc), file=sys.stderr)
            continue
        for item in items:
            item.setdefault("kind", "record")
            item["file_id"] = file_id
            item.setdefault("observed_at", row["captured_at"])
            snap = (item.get("attrs") or {}).get("snapshot_at")
            if item["kind"] == "record" and isinstance(snap, str) and snap > item["observed_at"]:
                item["observed_at"] = snap  # a row in a snapshot is known to exist at snapshot time
            item["captured_at"] = row["captured_at"]
            item["origin"] = row["origin"]
            if item["kind"] == "coverage":
                item["full_table"] = path.endswith(".sql")
            observations.append(item)
    if missing and verbose:
        print("WARN parser modules not available: %s" % ", ".join(sorted(missing)), file=sys.stderr)
    return observations, manifest
