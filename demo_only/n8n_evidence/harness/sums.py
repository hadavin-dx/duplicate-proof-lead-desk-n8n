"""NEW: SHA256SUMS of the published files (raw results, tables, workflows, lock file, the rule texts, the three explanatory files and the recorded hash of the unpublished original rule).
  python -I harness/sums.py write     writes SHA256SUMS      python -I harness/sums.py check     checks it"""
import hashlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
GROUPS = ("published_run/*.ndjson", "published_run/invalid/*.ndjson", "workflows/*.json", "tables.md", "package.json", "package-lock.json", "gate-rule.sha256.txt", "GATES.md", "REPRODUCTION-RULE.md", "GLOSSARY.md", "DETAILS.md", "CHANGES.md")


def files():
    return sorted({p for g in GROUPS for p in ROOT.glob(g)})


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build(root=ROOT):
    return "".join("%s  %s\n" % (digest(p), p.relative_to(ROOT).as_posix()) for p in files())


def check(sums_text, root=ROOT):
    """Returns the list of problems (empty = every listed file matches and no published file is missing from the list)."""
    bad, listed = [], set()
    for line in sums_text.splitlines():
        h, sep, name = line.partition("  ")
        if not sep or not name:
            sys.exit("SHA256SUMS has a malformed line (expected: hash, two spaces, file name)")
        listed.add(name)
        p = root / name
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != h:
            bad.append(name)
    bad += ["unlisted: " + p.relative_to(ROOT).as_posix() for p in files() if p.relative_to(ROOT).as_posix() not in listed]
    return bad


if __name__ == "__main__":
    if sys.argv[1:] == ["write"]:
        (ROOT / "SHA256SUMS").write_text(build(), encoding="utf-8", newline="\n")
    else:
        print(check((ROOT / "SHA256SUMS").read_text(encoding="utf-8")) or "ok")
