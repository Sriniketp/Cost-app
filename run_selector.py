"""Run selector data and run-specific DD/VDT revision utilities."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pandas as pd

from cost_allocation.sql_connection import SqlSettings, SqlSourceError, fetch_dataframe

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persistent disk cache for extracted DD files
# ---------------------------------------------------------------------------
_DD_CACHE_DIR = Path.home() / ".cache" / "cost_allocation" / "dd_cache"
_DD_CACHE_MAX_ENTRIES = 20
# Written last, after a run's DD files are extracted. A cache directory
# without it (for example after an interrupted extraction) is never reused.
_DD_CACHE_COMPLETE_MARKER = ".complete"
_VDT_CACHE_DIR = Path.home() / ".cache" / "cost_allocation" / "vdt_cache"

# ---------------------------------------------------------------------------
# uktm-dds repository location
# ---------------------------------------------------------------------------
# Each run's .RUN file, DD files and VDT are committed to uktm-dds with the run
# ID as the commit message. The app keeps its own bare mirror of the GitLab
# repo in the per-user cache, so no machine-specific path is needed.
DD_REPO_URL = "https://gitlab2.cbas.cloud/uk-times-off-sen/uktm-dds.git"
DD_REPO_URL_ENV_VAR = "UKTM_DDS_REPO_URL"
DD_DIR_ENV_VAR = "UKTM_DDS_DIR"  # optional: reuse an existing local clone
_DD_REPO_MIRROR_DIR = Path.home() / ".cache" / "cost_allocation" / "uktm-dds.git"
_DD_REPO_CLONE_TIMEOUT_SECONDS = 600


class DdRepoConfigError(ValueError):
    """Raised when the uktm-dds repository cannot be located or cloned."""


class RunDdUnavailableError(ValueError):
    """Raised when a run's commit, .RUN file or DD files cannot be resolved."""


def _is_git_repository(path: Path) -> bool:
    try:
        res = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def configured_dd_dir() -> Path:
    """Return a local git repository holding the uktm-dds history.

    ``UKTM_DDS_DIR`` may name an existing clone. Otherwise the app uses its own
    bare mirror of ``UKTM_DDS_REPO_URL`` (default: the GitLab uktm-dds repo),
    cloning it on first use with the user's normal git credentials.
    """
    override = os.getenv(DD_DIR_ENV_VAR, "").strip()
    if override:
        path = Path(override)
        if not _is_git_repository(path):
            raise DdRepoConfigError(f"{DD_DIR_ENV_VAR}={override} is not a git repository.")
        return path

    if _is_git_repository(_DD_REPO_MIRROR_DIR):
        return _DD_REPO_MIRROR_DIR
    return _clone_dd_mirror(os.getenv(DD_REPO_URL_ENV_VAR, "").strip() or DD_REPO_URL)


def _clone_dd_mirror(url: str) -> Path:
    """Clone *url* as a bare mirror into the cache, atomically."""
    if not url.startswith("https://"):
        raise DdRepoConfigError(f"{DD_REPO_URL_ENV_VAR} must be an https:// URL, got {url!r}.")

    _DD_REPO_MIRROR_DIR.parent.mkdir(parents=True, exist_ok=True)
    staging = _DD_REPO_MIRROR_DIR.with_name(f"{_DD_REPO_MIRROR_DIR.name}.{os.getpid()}.tmp")
    shutil.rmtree(staging, ignore_errors=True)
    log.info("Cloning %s into %s", url, _DD_REPO_MIRROR_DIR)
    try:
        res = subprocess.run(
            ["git", "clone", "--mirror", "--quiet", "--", url, str(staging)],
            capture_output=True, text=True, timeout=_DD_REPO_CLONE_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise DdRepoConfigError(f"Could not clone uktm-dds from {url}: {exc}") from exc
    if res.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        detail = (res.stderr or "").strip().splitlines()[-1:] or ["git clone failed"]
        raise DdRepoConfigError(
            f"Could not clone uktm-dds from {url}: {detail[0]}. "
            "Check GitLab access, or set UKTM_DDS_DIR to an existing clone."
        )

    try:
        staging.rename(_DD_REPO_MIRROR_DIR)
    except OSError as exc:
        # Another session finished cloning first; use its copy.
        shutil.rmtree(staging, ignore_errors=True)
        if not _is_git_repository(_DD_REPO_MIRROR_DIR):
            raise DdRepoConfigError(
                f"Could not place uktm-dds mirror at {_DD_REPO_MIRROR_DIR}"
            ) from exc
    return _DD_REPO_MIRROR_DIR

# ---------------------------------------------------------------------------
# RunLog retrieval
# ---------------------------------------------------------------------------

RUNLOG_QUERY = "EXECUTE [usr].[LoadTable] @TableName = ?, @RunList = ?"
RUN_GROUPS_QUERY = (
    "SELECT SortOrder, GroupCode, GroupName, Runs, Description FROM RunGroups"
)
_RUNLOG_REQUIRED_COLUMNS = {
    "runid",
    "objectives",
    "run_description",
    "ref_id",
    "run_date",
    "run_author",
}
_RUN_GROUP_REQUIRED_COLUMNS = {
    "sortorder",
    "groupcode",
    "groupname",
    "runs",
    "description",
}


def load_runlog(*, settings: SqlSettings | None = None) -> pd.DataFrame:
    """Load the SQL RunLog and return the selector's stable column contract.

    Returns columns: run_id, date, user, purpose, objective, ref_id.
    The DD file list is no longer sourced from the RunLog — it comes
    from the ``.RUN`` file in the uktm-dds repo at the run's commit.
    """
    rows = fetch_dataframe(
        RUNLOG_QUERY,
        ("RunLog", None),
        settings=settings,
    )
    if rows.empty:
        return pd.DataFrame(
            columns=["run_id", "date", "user", "purpose", "objective", "ref_id"]
        )

    normalized = rows.copy()
    normalized.columns = [str(column).casefold() for column in normalized.columns]
    missing = sorted(_RUNLOG_REQUIRED_COLUMNS - set(normalized.columns))
    if missing:
        raise SqlSourceError(
            "SQL RunLog schema is missing required columns: " + ", ".join(missing) + "."
        )

    raw_dates = normalized["run_date"].fillna("").astype(str).str.strip()
    iso_dates = raw_dates.str.match(r"^\d{4}-\d{2}-\d{2}", na=False)
    parsed_dates = pd.Series(pd.NaT, index=normalized.index, dtype="datetime64[ns]")
    parsed_dates.loc[iso_dates] = pd.to_datetime(
        raw_dates.loc[iso_dates], format="mixed", yearfirst=True, errors="coerce"
    )
    parsed_dates.loc[~iso_dates] = pd.to_datetime(
        raw_dates.loc[~iso_dates], format="mixed", dayfirst=True, errors="coerce"
    )

    df = pd.DataFrame(
        {
            "run_id": normalized["runid"].fillna("").astype(str).str.strip(),
            "date": parsed_dates,
            "user": normalized["run_author"],
            "purpose": normalized["run_description"],
            # Preserve the selector's established conversion to £bn.
            "objective": pd.to_numeric(normalized["objectives"], errors="coerce") / 1000,
            "ref_id": normalized["ref_id"],
        }
    )
    df = df.loc[df["run_id"].ne("")].copy()
    # A leading hash is the RunLog's established soft-delete marker. Keep
    # deleted runs out of every selector consumer, not just the Streamlit UI.
    df = df.loc[~df["run_id"].str.startswith("#")].copy()
    return df.sort_values(
        ["date", "run_id"], ascending=[False, False]
    ).reset_index(drop=True)


def load_run_groups(*, settings: SqlSettings | None = None) -> pd.DataFrame:
    """Load active SQL RunGroups in their Vistool display order.

    Returns columns: group_code, group_name, runs, description, sort_order.
    As in Vistool, a leading ``#`` on ``GroupCode`` marks a deleted group.
    """
    rows = fetch_dataframe(RUN_GROUPS_QUERY, (), settings=settings)
    if rows.empty:
        return pd.DataFrame(
            columns=["group_code", "group_name", "runs", "description", "sort_order"]
        )

    normalized = rows.copy()
    normalized.columns = [str(column).casefold() for column in normalized.columns]
    missing = sorted(_RUN_GROUP_REQUIRED_COLUMNS - set(normalized.columns))
    if missing:
        raise SqlSourceError(
            "SQL RunGroups schema is missing required columns: " + ", ".join(missing) + "."
        )

    groups = pd.DataFrame(
        {
            "group_code": normalized["groupcode"].fillna("").astype(str).str.strip(),
            "group_name": normalized["groupname"].fillna("").astype(str).str.strip(),
            "runs": normalized["runs"].fillna("").astype(str).str.strip(),
            "description": normalized["description"].fillna("").astype(str).str.strip(),
            "sort_order": pd.to_numeric(normalized["sortorder"], errors="coerce"),
        }
    )
    groups = groups.loc[
        groups["group_code"].ne("") & ~groups["group_code"].str.startswith("#")
    ].copy()
    return groups.sort_values(
        ["sort_order", "group_name", "group_code"],
        ascending=[True, True, True],
        na_position="last",
        kind="stable",
    ).reset_index(drop=True)


def parse_run_group_members(runs: object) -> list[str]:
    """Return unique active run IDs from one comma-separated RunGroups value.

    Follows Vistool's ``ParseGroupRunsSpec``: each token is either a bare run
    ID or ``'Label' = run_id`` / ``'Label' ~= run_id``. The label (and
    Vistool's ``~=`` "loaded but hidden" flag) only affects Vistool display,
    so only the run ID after the ``=`` is kept.
    """
    try:
        if pd.isna(runs):
            return []
    except (TypeError, ValueError):
        pass
    members: list[str] = []
    for token in str(runs).split(","):
        # Run IDs never contain "=", so the last one separates label from ID.
        run_id = token.rsplit("=", 1)[-1].strip()
        if run_id and not run_id.startswith("#") and run_id not in members:
            members.append(run_id)
    return members


# ---------------------------------------------------------------------------
# .RUN file — DD list extraction (source of truth)
# ---------------------------------------------------------------------------

_BATINCLUDE_RE = re.compile(r"^\$BATINCLUDE\s+([^\s]+)", re.IGNORECASE)


def _parse_batincludes(run_file_text: str) -> list[str]:
    """Extract ordered DD file stems from $BATINCLUDE lines in .RUN text."""
    out: list[str] = []
    for raw in run_file_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _BATINCLUDE_RE.match(line)
        if m:
            fname = m.group(1).strip()
            stem = Path(fname).stem.lower()
            # Only include .dd files (skip .mod etc.)
            if fname.lower().endswith(".dd"):
                out.append(stem)
    return out


def dd_list_to_scenario_order(dd_list: list[str]) -> dict[str, int]:
    """Convert an ordered DD file list to a scenario precedence dict.

    Parameters
    ----------
    dd_list:
        Ordered list of DD file stems (as returned by
        ``extract_dd_list_from_run_file``).

    Returns
    -------
    dict[str, int]
        Mapping of lowercase scenario name → 1-based precedence rank.
    """
    return {sc.lower(): i + 1 for i, sc in enumerate(dd_list)}


def _resolve_commit_and_dd_list(
    run_id: str,
    dd_dir: Path,
) -> tuple[str | None, list[str] | None]:
    """Find the commit for *run_id* and parse the DD list from its .RUN file."""
    commit = _git_commit_for_run(run_id, dd_dir)
    if commit is None:
        return None, None

    run_filename = _find_run_file_at_commit(run_id, commit, dd_dir)
    if run_filename is None:
        log.warning("No .RUN file found for %s at commit %s", run_id, commit[:8])
        return None, None

    try:
        result = subprocess.run(
            ["git", "-C", str(dd_dir), "show", f"{commit}:{run_filename}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or not result.stdout:
            return None, None
    except Exception:
        return None, None

    dd_list = _parse_batincludes(result.stdout)
    if not dd_list:
        return None, None
    return commit, dd_list


def extract_dd_list_from_run_file(
    run_id: str,
    dd_dir: Path,
) -> list[str] | None:
    """Extract the DD file list from the .RUN file at the run's git commit.

    Returns
    -------
    list[str] or None
        Ordered list of DD file stems (lowercase, no .dd extension),
        or *None* if the commit or .RUN file cannot be found.
    """
    _, dd_list = _resolve_commit_and_dd_list(run_id, dd_dir)
    return dd_list


_RESOLVED_COMMITS: dict[tuple[str, str], str] = {}
_RESOLVED_COMMITS_MAX = 64


def forget_resolved_commits() -> None:
    """Drop memoised run-ID -> commit lookups (e.g. after a repo refresh)."""
    _RESOLVED_COMMITS.clear()


def _git_commit_for_run(run_id: str, dd_dir: Path) -> str | None:
    """Find the git commit in the uktm-dds repo whose message matches *run_id*.

    Searches all local refs first; if nothing is found, fetches from all
    remotes and retries so that commits on branches not yet tracked
    locally are still discoverable.

    Successful lookups are memoised per process: every run load otherwise
    repeats a ``git log --all --grep`` over the whole history. Misses are not
    memoised, so a run pushed later is still found via the fetch-and-retry.
    """
    key = (run_id, str(dd_dir))
    cached = _RESOLVED_COMMITS.get(key)
    if cached is not None:
        return cached
    commit = _lookup_git_commit_for_run(run_id, dd_dir)
    if commit is not None:
        if len(_RESOLVED_COMMITS) >= _RESOLVED_COMMITS_MAX:
            _RESOLVED_COMMITS.pop(next(iter(_RESOLVED_COMMITS)))
        _RESOLVED_COMMITS[key] = commit
    return commit


def _lookup_git_commit_for_run(run_id: str, dd_dir: Path) -> str | None:
    def _search() -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(dd_dir), "log", "--all",
                 "--regexp-ignore-case",
                 f"--grep=^{run_id}$", "--format=%H", "--max-count=1"],
                capture_output=True, text=True, timeout=15,
            )
            hashes = result.stdout.strip().splitlines()
            return hashes[0] if hashes else None
        except Exception:
            return None

    hit = _search()
    if hit:
        return hit

    # Commit not found — fetch all remotes and retry.
    try:
        subprocess.run(
            ["git", "-C", str(dd_dir), "fetch", "--all", "--quiet"],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass
    return _search()


def _find_run_file_at_commit(
    run_id: str, commit: str, dd_dir: Path,
) -> str | None:
    """Return the actual .RUN filename at *commit*, handling case mismatches.

    Git is case-sensitive, so ``CB6_DD_114.RUN`` ≠ ``CB6_dd_114.RUN``.
    List the tree and find the correct name via case-insensitive match.
    """
    # Fast path: try the exact name first.
    exact = f"{run_id}.RUN"
    try:
        res = subprocess.run(
            ["git", "-C", str(dd_dir), "show", f"{commit}:{exact}"],
            capture_output=True, text=True, timeout=10,
        )
        if res.returncode == 0 and res.stdout:
            return exact
    except Exception:
        pass

    # Slow path: list the tree and match case-insensitively.
    try:
        res = subprocess.run(
            ["git", "-C", str(dd_dir), "ls-tree", "--name-only", commit],
            capture_output=True, text=True, timeout=10,
        )
        if res.returncode == 0:
            target = f"{run_id}.run"
            for entry in res.stdout.splitlines():
                if entry.strip().lower() == target:
                    return entry.strip()
    except Exception:
        pass

    return None


def _find_vdt_file_at_commit(commit: str, dd_dir: Path) -> str | None:
    """Return the sole VDT artefact committed with a run, if unambiguous.

    A run must use the topology from its own git revision.  When a commit
    contains multiple VDT files there is no reliable selection directive in
    the ``.RUN`` file, so this deliberately returns ``None`` rather than
    silently choosing an arbitrary current-model VDT.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(dd_dir), "ls-tree", "-r", "--name-only", commit],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if res.returncode != 0:
        return None
    candidates = [
        name.strip() for name in res.stdout.splitlines()
        if name.strip().lower().endswith(".vdt")
    ]
    if len(candidates) != 1:
        if candidates:
            log.warning("Ambiguous VDT files at commit %s: %s", commit[:8], candidates)
        else:
            log.warning("No VDT file at commit %s", commit[:8])
        return None
    return candidates[0]


def extract_vdt_file_at_commit(
    run_id: str,
    dd_dir: Path,
) -> Path | None:
    """Extract the selected run's sole committed VDT into the local cache."""
    commit = _git_commit_for_run(run_id, dd_dir)
    if commit is None:
        return None
    vdt_filename = _find_vdt_file_at_commit(commit, dd_dir)
    if vdt_filename is None:
        return None

    cache_dir = _VDT_CACHE_DIR / _cache_key(run_id, commit)
    cached = cache_dir / Path(vdt_filename).name
    if cached.is_file():
        return cached

    try:
        res = subprocess.run(
            ["git", "-C", str(dd_dir), "show", f"{commit}:{vdt_filename}"],
            capture_output=True, text=True, timeout=20,
        )
        if res.returncode != 0 or not res.stdout:
            return None
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(res.stdout, encoding="utf-8")
        return cached
    except OSError:
        return None


def _evict_dd_cache() -> None:
    """Keep only the most recent entries in the DD disk cache."""
    if not _DD_CACHE_DIR.exists():
        return
    entries = sorted(_DD_CACHE_DIR.iterdir(), key=lambda p: p.stat().st_mtime)
    while len(entries) > _DD_CACHE_MAX_ENTRIES:
        old = entries.pop(0)
        shutil.rmtree(old, ignore_errors=True)


def _cache_key(run_id: str, commit: str) -> str:
    """Deterministic cache directory name for a (run_id, commit) pair."""
    h = hashlib.sha256(f"{run_id}:{commit}".encode()).hexdigest()[:12]
    return f"{run_id}_{h}"


def extract_dd_files_at_commit(
    run_id: str,
    dd_dir: Path,
) -> tuple[Path | None, list[str] | None]:
    """Extract DD files at the git commit corresponding to *run_id*.

    Uses ``git archive`` to extract all needed DD files in a single
    command.  Results are cached on disk so repeat loads skip git
    entirely.

    Returns
    -------
    tuple[Path | None, list[str] | None]
        (directory, dd_file_list) — the directory containing the
        extracted DD files, and the ordered DD file list parsed from
        the .RUN file.  Both are *None* if the commit or .RUN file
        cannot be found.
    """
    commit, dd_list = _resolve_commit_and_dd_list(run_id, dd_dir)
    if commit is None:
        return None, None

    # --- Check persistent disk cache ---
    cache_name = _cache_key(run_id, commit)
    cached_dir = _DD_CACHE_DIR / cache_name
    if (cached_dir / _DD_CACHE_COMPLETE_MARKER).is_file():
        log.info("DD cache hit for %s at %s", run_id, commit[:8])
        return cached_dir, dd_list

    # --- Extract via single git archive command ---
    out_dir = cached_dir
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    dd_filenames = [f"{name}.dd" for name in dd_list]
    try:
        res = subprocess.run(
            ["git", "-C", str(dd_dir), "archive", "--format=tar", commit, "--"]
            + dd_filenames,
            capture_output=True, timeout=30,
        )
        if res.returncode == 0 and res.stdout:
            with tarfile.open(fileobj=io.BytesIO(res.stdout), mode="r:") as tf:
                # Only extract regular files whose names are in our set
                safe_names = set(dd_filenames)
                for member in tf.getmembers():
                    if member.name in safe_names and member.isreg():
                        member.name = Path(member.name).name  # strip any path
                        tf.extract(member, path=out_dir, filter="data")
        else:
            # git archive may fail if some files don't exist at this commit.
            # Fall back to individual git show calls for each file.
            log.warning(
                "git archive failed for %s (rc=%d), falling back to git show",
                run_id, res.returncode,
            )
            for fname in dd_filenames:
                try:
                    r = subprocess.run(
                        ["git", "-C", str(dd_dir), "show", f"{commit}:{fname}"],
                        capture_output=True, text=True, timeout=10,
                    )
                    if r.returncode == 0 and r.stdout:
                        (out_dir / fname).write_text(r.stdout, encoding="utf-8")
                    else:
                        log.warning("DD file missing at commit %s: %s", commit[:8], fname)
                except Exception:
                    log.warning("Failed to extract DD file %s", fname)
    except Exception as exc:
        log.warning("git archive failed for %s: %s", run_id, exc)
        shutil.rmtree(out_dir, ignore_errors=True)
        return None, None

    extracted = sum(1 for name in dd_filenames if (out_dir / name).is_file())
    if extracted > 0:
        (out_dir / _DD_CACHE_COMPLETE_MARKER).write_text(commit, encoding="utf-8")
        _evict_dd_cache()
        return out_dir, dd_list

    # Nothing extracted — clean up
    shutil.rmtree(out_dir, ignore_errors=True)
    return None, None
