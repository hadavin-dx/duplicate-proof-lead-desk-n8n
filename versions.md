# versions.md

The versions the demo was run with, on one Windows computer. Nothing here is a claim about other machines.

## Runtime
| Item | Value | How read |
|---|---|---|
| Python used on the Windows computer | 3.13.3 (MSC v.1943, 64 bit) | `python --version` |
| Python required | 3.12 or newer (`requires-python`). 3.12 is the lowest version the package allows; the full test suite was run on 2026-11-12: Windows with Python 3.13 2449 passed, 15 skipped; Linux with Python 3.12 (a container) 2462 passed, 2 skipped; no test failed on either (the logs are not shipped). The skipped tests need another operating system, a tool that machine did not have, or the load measurement. Edits made after those runs changed only documentation and one test file, which was run on its own. | pyproject.toml |
| SQLite library | 3.49.1 (needs 3.24 or newer for `ON CONFLICT ... DO UPDATE`) | `sqlite3.sqlite_version` |
| Runtime dependencies | none (Python standard library only) | pyproject.toml |

## n8n (demo only)
| Item | Value | How read |
|---|---|---|
| n8n | 2.46.6, regular mode (no queue), one process, SQLite inside n8n | the n8n runs, once |
| Node.js | 24.22.2 | same runs |

This is the single home of the n8n version; the README and `docs/n8n-recipe.md` point here. n8n is not part of this repository and not a dependency of the desk. Not checked: that anyone else's n8n install matches these numbers ; the recipe is not claimed to work on other versions.

## Live model step (demo only)
| Item | Value |
|---|---|
| `anthropic-version` request header (`LIVE_API_VERSION`) | 2026-10-21 |

The live step has only been run against the local stand-in server.

## GitHub Actions used by `.github/workflows/` (each pinned by its full commit SHA in the workflow file)
| Action | Version |
|---|---|
| `actions/checkout` | v7.0.1 |
| `actions/setup-python` | v7.1.0 |
| `github/codeql-action` (`init`, `analyze`) | v4.39.3 |

The commit SHAs above and the gitleaks download checksum in `lint.yml` were checked against the upstream repositories on 2026-11-12: each tag resolves to the pinned commit, and the gitleaks 8.30.1 archive matches the checksum.

## Development tools (pinned with hashes, installed with `pip install --require-hashes`)
Direct pins live in `requirements-dev.in`. Two lock files, same versions: `requirements-dev.txt` (the authority: Linux x86_64, Python 3.12.16, made in the throwaway container `python:3.12-slim` with `pip-compile --generate-hashes --allow-unsafe --strip-extras`, pip-tools 7.6.2, a build helper that is not itself pinned) and `requirements-dev-win.txt` (Windows, Python 3.13; made with uv 0.12.21: `uv pip compile requirements-dev.in --generate-hashes --python-version 3.13 --python-platform windows --no-strip-extras`). The only difference is `colorama` (a Windows-only transitive of pytest), which is in the Windows file and not in the Linux file. Neither file works on the other OS.

| Package | Version | Licence (as `pip-licenses` reads it, see below) | Used for |
|---|---|---|---|
| pytest | 9.2.0 | MIT | tests |
| ruff | 0.17.4 | MIT | lint and format; rules E, F, W, I, S, PLR2004 |
| bandit | 1.9.4 | Apache-2.0 | `bandit -ll -r src tools scripts -c pyproject.toml` (as in `.github/workflows/lint.yml`) |
| mypy | 2.4.1 | MIT | `mypy --platform linux` and `mypy --platform win32` (strict, `src/` and `tools/`; both as in `.github/workflows/lint.yml`) |
| iniconfig 2.3.1, pluggy 1.7.0, mypy-extensions 1.2.0, librt 0.16.0, ast-serialize 0.13.1, rich 15.0.0, markdown-it-py 4.2.0, mdurl 0.1.2, pyyaml 6.0.3 | as shown | MIT (markdown-it-py and mdurl: classifier says MIT) | transitive |
| packaging 26.3 | | Apache-2.0 OR BSD-2-Clause | transitive |
| pygments 2.21.0 | | BSD-2-Clause | transitive |
| colorama 0.4.6 | | BSD (classifier) | transitive on Windows only; in `requirements-dev-win.txt`, not in the Linux lock |
| stevedore 5.9.1 | | Apache-2.0 | transitive |
| typing-extensions 4.16.0 | | PSF-2.0 | transitive |
| pathspec 1.1.1 | | **MPL-2.0** (classifier) | transitive of mypy. MPL-2.0 is file-level copyleft, not GPL, AGPL, SSPL or BUSL; used only as a development tool and not shipped. |

### Browser tests (optional, a separate lock)
`requirements-browser.in` lists one direct pin: `playwright==1.63.0`. Two lock files made with uv 0.11.32, each with hashes and each held to the pins of the matching development lock (`-c`), so installing it after the development lock changes no development pin: `requirements-browser.txt` (`uv pip compile requirements-browser.in --generate-hashes --python-version 3.12 --python-platform linux --no-strip-extras --exclude-newer 2026-11-12 -c requirements-dev.txt -o requirements-browser.txt`) and `requirements-browser-win.txt` (the same with `--python-version 3.13 --python-platform windows`, `-c requirements-dev-win.txt` and `-o requirements-browser-win.txt`). `--exclude-newer` ignores any release uploaded after that date, so the same command gives the same lock later. Install: `pip install --require-hashes -r requirements-browser.txt` after `requirements-dev.txt`. The checks in `.github/workflows/lint.yml` do not install it (no browser job yet).

Side effect of the review-tool settings: `.codeant/configuration.json` lists both browser lock files (`requirements-browser.txt`, `requirements-browser-win.txt`) among the files its review bot skips, next to the two development locks, because their content is hash lines. So the bot's check of dependencies for known vulnerabilities no longer reads these two files; it still reads `requirements-browser.in`, which names `playwright==1.63.0`, but not the three packages that only the lock lists (greenlet, pyee; typing-extensions is also in the development lock).

Secret-scan allowance for the browser lock files: the entry in `.gitleaks.toml` accepts a 64-character hash line only in a file named `requirements-browser.txt` or `requirements-browser-win.txt` at the repository root (`paths = ['''^requirements-browser(-win)?\.txt$''']`). The secret scan in `tests/test_hygiene.py` runs from the repository root, so the path is relative and a copy in a sub-folder is not allowed: a planted hash line in a file of that name inside a sub-folder is reported by `gitleaks` (tried with gitleaks 8.30.1 on this tree: one finding), and the second check in the same test (the pattern list minus `exceptions.txt`, which is also root-anchored) reports it as well.

| Package | Version | Licence (the package's own metadata) | Used for |
|---|---|---|---|
| playwright | 1.63.0 | Apache-2.0 | drives the browser in the browser tests |
| greenlet 3.5.6, pyee 13.0.1 | | MIT AND PSF-2.0 (greenlet), MIT (pyee classifier) | transitive of playwright |
| typing-extensions 4.16.0 | | PSF-2.0 | transitive of pyee; the same pin as in the development lock |

These licences were read from the installed packages' metadata on 2026-11-12; the full licence scan described below was not repeated for this file. Playwright's package also contains a bundled Node program (its driver) with its own third-party notices; those were not examined.

Licences were scanned on 2026-11-12 with `pip-licenses` 5.5.5 (`--from=mixed`: the package's own licence field, and its Trove classifier where the field is empty), once for each lock file, each installed with `--require-hashes` into a clean environment: `requirements-dev-win.txt` into a Python 3.13.3 environment on Windows (19 packages, all listed above), and `requirements-dev.txt` into the `python:3.12-slim` container (Python 3.12.16, 18 packages: the same minus `colorama`). `pip-licenses` itself is installed separately for the scan and is not in either lock file. In both runs every package matched this table, `ast-serialize` and `librt` are MIT, and the only copyleft-family licence is `pathspec` (MPL-2.0, file-level, development tool only, not shipped). The scan reads each package's own metadata, not its project page. The raw outputs are kept with the author's records and are not shipped.

## Not checked
- Windows restart on a fixed port: `SO_EXCLUSIVEADDRUSE` plus TIME_WAIT (about 2 minutes) may make a restart bind fail, and the conflicting bind may raise `PermissionError` (WSAEACCES) instead of `EADDRINUSE`. The tests avoid it by using port 0 (the system picks a free port).
- The hashes in `requirements-dev.txt` are for all files PyPI listed when it was made; `requirements-dev.txt` verified by a successful `pip install --require-hashes` in a clean `python:3.12-slim` container (Linux x86_64, Python 3.12.16, SQLite 3.46.1). `requirements-dev-win.txt` verified by `pip install --require-hashes` into a throwaway venv on the Windows laptop (Python 3.13.3). Not tested: other Linux architectures, the GitHub CI runner image.
- bandit B101 on tests: tests are excluded from bandit, so not relevant.
- gitleaks is a separate program (version and checksum pinned in `.github/workflows/lint.yml`), not part of the lock files. pre-commit and exiftool are not used.
