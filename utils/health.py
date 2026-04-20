"""Health and readiness probes for the NBA Data Ingestion Pipeline.

These synthesise liveness and readiness snapshots from the filesystem and
the :mod:`config` module without instantiating any collaborator. They are
consumed by ``run.py health`` and ``run.py ready``.

Public API
----------
``check_health() -> Dict[str, Any]``
    Liveness probe. Returns a small dict indicating the process is up
    and which Python interpreter version is running. Must be cheap and
    side-effect free (it never touches the filesystem or the network).

``check_readiness() -> Dict[str, Any]``
    Readiness probe. Returns a dict of the shape
    ``{"status": "ready"|"not_ready", "timestamp": "<ISO-8601>",
    "checks": {...}}`` after verifying:

    * ``output_dir_writable``     — can a temporary file be created under
      ``config.OUTPUT_DIR``?
    * ``required_headers_present`` — does ``config.REQUIRED_HEADERS``
      include ``Referer`` and ``User-Agent`` (Rule 3 minimum)?
    * ``rate_limit_configured``   — is ``config.RATE_LIMIT_SECONDS`` a
      finite number ``>= 1.0`` (Rule 2 floor)?
    * ``checkpoint_parseable``    — if ``config.CHECKPOINT_PATH`` exists,
      does it parse as a JSON object (Rule 5 resumability)?

    The return dict shape is stable regardless of outcome so that
    dashboards and scripts can parse it deterministically.

Design notes
------------
* **Folder-brief rule: no collaborator instantiation.** This module
  reads ``config.*`` and the filesystem directly. In particular, the
  ``_probe_checkpoint`` helper parses ``config.CHECKPOINT_PATH`` with
  :func:`json.loads` rather than instantiating
  :class:`utils.checkpoint.CheckpointManager`. That keeps the readiness
  probe a pure filesystem + config reader and avoids the import-time
  cost of pulling in ``utils.logger`` / ``utils.correlation`` on every
  ``run.py health`` invocation.
* **Probes never raise.** Every private ``_probe_*`` helper catches the
  exceptions it can legitimately encounter (e.g. :class:`OSError`,
  :class:`json.JSONDecodeError`) and encodes the failure inside the
  returned dict. :func:`check_health` and :func:`check_readiness` thus
  always return a dict of the documented shape and the caller (``run.py``)
  can decide its exit code based on ``status`` alone.
* **One intentional side effect.** ``_probe_output_writable`` calls
  ``config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)`` before
  attempting the temporary-file probe. That side effect is idempotent
  and conservative: it ensures a first-time operator on a fresh clone
  gets a ``ready`` verdict without first having to execute a pipeline
  run solely to materialise the output directory. All other probes are
  strictly read-only.
* **Rule 2 floor verification belongs here.** Even though the rate
  limiter enforces the floor at request time, surfacing the config
  violation up-front through ``run.py ready`` gives the operator a
  diagnostic handle before any HTTPS traffic is issued — useful when
  ``NBA_RATE_LIMIT_SECONDS`` has been set too low in the environment.
* **Stable ISO-8601 UTC timestamps.** Both ``check_health`` and
  ``check_readiness`` emit timestamps via
  ``datetime.now(timezone.utc).isoformat(timespec="seconds")`` so log
  aggregators and dashboards can correlate probe results across hosts
  regardless of their local timezone.
* **Liveness and readiness are kept separate.** Following
  Kubernetes-style conventions: liveness ("still running") and
  readiness ("can accept traffic / do useful work") answer different
  operational questions and carry different remediation implications.
  We preserve that distinction even in this single-process CLI so
  operators can script against them predictably.
"""

import json
import platform
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import config


# =============================================================================
# Public API — liveness probe
# =============================================================================


def check_health() -> Dict[str, Any]:
    """Return a liveness snapshot of the process.

    This probe is intentionally cheap: it never touches the filesystem or
    the network. If the interpreter can execute this function at all, the
    process is alive, so the return value always carries
    ``"status": "ok"``.

    Returns
    -------
    Dict[str, Any]
        A dict with the stable shape::

            {
                "status": "ok",
                "timestamp": "<ISO-8601 UTC with seconds precision>",
                "python_version": "<e.g. '3.12.3'>",
                "component": "nba-data-ingestion-pipeline",
            }

        ``status`` comes first by convention so that visual inspection of
        the JSON output immediately shows the verdict. ``python_version``
        helps operators diagnose multi-version environments (system
        Python vs. project ``.venv``), a pitfall called out in
        ``docs/ONBOARDING.md``.
    """
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python_version": platform.python_version(),
        "component": "nba-data-ingestion-pipeline",
    }


# =============================================================================
# Public API — readiness probe
# =============================================================================


def check_readiness() -> Dict[str, Any]:
    """Return a readiness snapshot of the process.

    Runs the four readiness probes (output-dir writability, required
    headers presence, rate-limit floor, checkpoint parseability) and
    aggregates their verdicts into a top-level status. The dict shape is
    stable regardless of outcome so downstream tooling (dashboards,
    monitoring scripts) can parse it deterministically.

    Returns
    -------
    Dict[str, Any]
        A dict with the stable shape::

            {
                "status": "ready" | "not_ready",
                "timestamp": "<ISO-8601 UTC with seconds precision>",
                "checks": {
                    "output_dir_writable":       {"status": "ok"|"fail", "detail": "<str>"},
                    "required_headers_present":  {"status": "ok"|"fail", "detail": "<str>"},
                    "rate_limit_configured":     {"status": "ok"|"fail", "detail": "<str>"},
                    "checkpoint_parseable":      {"status": "ok"|"fail", "detail": "<str>"},
                },
            }

        ``status`` is ``"ready"`` if and only if every nested
        ``checks[*].status`` equals ``"ok"``; otherwise it is
        ``"not_ready"``. No probe ever raises, so this function never
        raises.
    """
    checks: Dict[str, Dict[str, Any]] = {
        "output_dir_writable": _probe_output_writable(),
        "required_headers_present": _probe_required_headers(),
        "rate_limit_configured": _probe_rate_limit(),
        "checkpoint_parseable": _probe_checkpoint(),
    }

    overall = "ready" if all(c["status"] == "ok" for c in checks.values()) else "not_ready"

    return {
        "status": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks": checks,
    }


# =============================================================================
# Private probe helpers
# =============================================================================


def _probe_output_writable() -> Dict[str, Any]:
    """Verify that ``config.OUTPUT_DIR`` exists and accepts new files.

    Creates the directory if it is missing (idempotent; the only side
    effect in this module), then uses :class:`tempfile.NamedTemporaryFile`
    as a context manager to create a small probe file, write a few bytes,
    flush, and delete the file on context exit. This exercises both the
    directory's permission bits and the filesystem's quota / inode state
    without leaving artifacts behind.

    Any exception encountered (``PermissionError``, ``OSError``,
    filesystem-full, unexpected path type, etc.) is captured into the
    returned dict; the probe never raises. This is the single place in
    the module where a broad ``except Exception`` is permitted — see the
    module docstring ("Probes never raise").

    Returns
    -------
    Dict[str, Any]
        ``{"status": "ok", "detail": <str>}`` on success;
        ``{"status": "fail", "detail": "<ExceptionType>: <message>"}``
        on failure.
    """
    try:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=config.OUTPUT_DIR,
            delete=True,
            prefix=".probe_",
            suffix=".tmp",
        ) as handle:
            handle.write(b"probe")
            handle.flush()
        return {
            "status": "ok",
            "detail": f"Wrote and deleted probe file under {config.OUTPUT_DIR}",
        }
    except Exception as exc:  # noqa: BLE001 - intentional: probes must never raise
        return {"status": "fail", "detail": f"{type(exc).__name__}: {exc}"}


def _probe_required_headers() -> Dict[str, Any]:
    """Verify that ``config.REQUIRED_HEADERS`` satisfies Rule 3 minimums.

    Rule 3 (see ``docs/New_Product_Prompt_20260418.md`` §5) requires that
    every outbound request carry a ``Referer`` header pinned to
    ``https://stats.nba.com`` and a browser-like ``User-Agent``. This
    probe verifies the *presence* of those keys in the config-declared
    header mapping. It deliberately does not validate the exact string
    values, so operators remain free to tune the ``User-Agent`` string
    (e.g. to a more recent browser) without tripping the readiness probe.

    Uses :func:`getattr` with a ``None`` default so that a missing
    attribute is reported as a structured ``fail`` status rather than an
    :class:`AttributeError`.

    Returns
    -------
    Dict[str, Any]
        ``{"status": "ok", "detail": "<N> headers configured"}`` on
        success; otherwise ``{"status": "fail", "detail": "<reason>"}``.
    """
    headers = getattr(config, "REQUIRED_HEADERS", None)
    if not isinstance(headers, dict) or not headers:
        return {
            "status": "fail",
            "detail": "config.REQUIRED_HEADERS missing or not a dict",
        }
    missing = [k for k in ("Referer", "User-Agent") if k not in headers]
    if missing:
        return {
            "status": "fail",
            "detail": f"Missing required header keys: {missing}",
        }
    return {
        "status": "ok",
        "detail": f"{len(headers)} headers configured",
    }


def _probe_rate_limit() -> Dict[str, Any]:
    """Verify that ``config.RATE_LIMIT_SECONDS`` satisfies the Rule 2 floor.

    Rule 2 (see ``docs/New_Product_Prompt_20260418.md`` §5) requires a
    minimum 1.0-second delay between consecutive outbound requests. The
    runtime rate limiter (``utils/rate_limiter.py``) enforces the floor
    at request time; surfacing the same check here gives operators a
    diagnostic handle *before* any HTTPS traffic is issued, useful when
    ``NBA_RATE_LIMIT_SECONDS`` has been set too low via environment
    override.

    Uses :func:`getattr` with a ``None`` default so that a missing
    attribute is reported as a structured ``fail`` status rather than an
    :class:`AttributeError`. Booleans are ``int`` subclasses in Python
    but are explicitly rejected to avoid surprises (``True`` would
    otherwise compare ``>= 1.0`` and pass).

    Returns
    -------
    Dict[str, Any]
        ``{"status": "ok", "detail": "RATE_LIMIT_SECONDS=<value>"}`` when
        the constant is a number ``>= 1.0``; otherwise a structured
        ``fail`` with a human-readable diagnostic.
    """
    value = getattr(config, "RATE_LIMIT_SECONDS", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return {
            "status": "fail",
            "detail": "config.RATE_LIMIT_SECONDS missing or not numeric",
        }
    if value < 1.0:
        return {
            "status": "fail",
            "detail": f"RATE_LIMIT_SECONDS={value} violates Rule 2 floor (>= 1.0)",
        }
    return {
        "status": "ok",
        "detail": f"RATE_LIMIT_SECONDS={value}",
    }


def _probe_checkpoint() -> Dict[str, Any]:
    """Verify that ``config.CHECKPOINT_PATH`` is parseable if it exists.

    Rule 5 (see ``docs/New_Product_Prompt_20260418.md`` §5) makes the
    ``checkpoint.json`` manifest the resumability pivot for every
    pipeline. This probe confirms that the manifest, if present, is a
    valid JSON object so pipelines can safely resume from it. The
    *absence* of the file is treated as ``ok`` — it simply indicates a
    fresh run.

    The probe parses the file directly with :func:`json.loads` rather
    than instantiating :class:`utils.checkpoint.CheckpointManager` (per
    the folder-brief rule "does NOT instantiate collaborators"). This
    keeps the probe a pure filesystem + config reader and avoids
    pulling in ``utils.logger`` / ``utils.correlation`` at module load
    time.

    Captures :class:`OSError` (I/O failure, encoding surprise) and
    :class:`json.JSONDecodeError` (malformed JSON) explicitly and
    converts them into a structured ``fail`` result. It also rejects
    JSON documents whose top level is not an object (``dict``) — the
    ``CheckpointManager`` contract (AAP §0.4.1.1) expects a mapping of
    domain → completed-keys.

    Returns
    -------
    Dict[str, Any]
        ``{"status": "ok", "detail": "No checkpoint file (fresh run)"}``
        when the file does not exist;
        ``{"status": "ok", "detail": "Checkpoint parsed; <N> domains tracked"}``
        when the file exists and parses as a dict;
        ``{"status": "fail", "detail": "<reason>"}`` otherwise.
    """
    path = Path(config.CHECKPOINT_PATH)
    if not path.exists():
        return {"status": "ok", "detail": "No checkpoint file (fresh run)"}
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "fail", "detail": f"{type(exc).__name__}: {exc}"}
    if not isinstance(parsed, dict):
        return {
            "status": "fail",
            "detail": f"Checkpoint top-level is {type(parsed).__name__}, expected dict",
        }
    return {
        "status": "ok",
        "detail": f"Checkpoint parsed; {len(parsed)} domains tracked",
    }
