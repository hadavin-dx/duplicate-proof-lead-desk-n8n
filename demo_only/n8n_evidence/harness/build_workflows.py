"""NEW: writes the workflow JSON files of workflows/ from the original generators (mkwf2.py, mkwf3.py), passed through
the repository's own scrub (tools/scrub_n8n_export.py: allow-listed keys only, so no ids, no credentials, no webhook ids).
Run from the repository root: python -I harness/build_workflows.py demo_only/n8n_evidence/workflows"""
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))  # repository root, for tools/
os.environ.setdefault("N8N_EVD_WORK", "")
import mkwf2  # noqa: E402
import mkwf3  # noqa: E402
from tools.scrub_n8n_export import scrub  # noqa: E402

out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
flows = {
    "variant-a-remove-duplicates": mkwf2.wf_dedupe("g2-a", "x"),
    "variant-d-remove-duplicates-limit-1": mkwf2.wf_dedupe("g2-d", "x"),  # same nodes as a; the limit is an n8n setting, not in the file
    "variant-b-postgres-insert": mkwf2.wf_pg("g2-b2", "x", ("", "")),
    "variant-c-data-table-upsert": mkwf2.wf_upsert("g2-c2", "x", "TABLE-ID", "={{ new Date($json.createdAt).getTime() === new Date($json.updatedAt).getTime() }}"),
    "variant-e-status-column": mkwf3.wf_e("e3", "x"),
    "variant-e-resend-job": mkwf3.wf_sweeper("x"),
}
for name, wf in flows.items():
    (out / (name + ".json")).write_text(json.dumps(scrub(wf), indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
print("wrote", len(flows), "workflows")
