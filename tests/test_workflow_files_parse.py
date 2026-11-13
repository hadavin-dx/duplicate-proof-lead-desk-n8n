"""Every GitHub Actions workflow file must be valid YAML with the shape GitHub needs.

Why: a plain `run:` value that contains a colon followed by a space (for example `--only-binary=:all: -r`) is NOT
valid YAML. GitHub then shows the run as failed with no jobs, and a check can stay "queued" for ever. No other test
parsed the files. PyYAML is in the development lock (requirements-dev.txt), so the check needs no new dependency.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))


def test_the_workflow_folder_is_not_empty() -> None:
    assert WORKFLOWS, "no workflow file found: the check below would pass for nothing"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_a_workflow_file_is_valid_yaml_with_jobs_and_steps(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        pytest.fail(f"{path.name} is not valid YAML, so GitHub cannot start it: {e}")
    assert isinstance(doc, dict), f"{path.name}: the top level must be a mapping"
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{path.name}: no jobs"
    for name, job in jobs.items():
        assert isinstance(job, dict), f"{path.name}: job {name} is not a mapping"
        steps = job.get("steps")
        assert isinstance(steps, list) and steps, f"{path.name}: job {name} has no steps"
        for i, step in enumerate(steps):
            assert isinstance(step, dict) and ("run" in step or "uses" in step), (
                f"{path.name}: job {name}, step {i + 1} has neither run nor uses"
            )
