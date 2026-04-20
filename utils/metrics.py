"""In-process Prometheus-style counter and histogram registry.

This module exposes a single module-level :data:`registry` singleton that
pipeline modules, the HTTP client, and the CLI use to record observations.
The CLI subcommand ``run.py metrics`` prints the Prometheus text-format
exposition via :meth:`MetricsRegistry.render_prometheus`.

The registry is thread-safe (guarded by a :class:`threading.Lock`) and
uses only the Python standard library — no ``prometheus_client``, no
``statsd``, no third-party dependency. This honours the dependency
budget established by Agent Action Plan §0.3 (only ``requests``,
``pandas``, ``click``, ``tenacity`` are runtime dependencies) and
preserves the stdlib-only mandate for ``utils/`` modules.

Design overview
---------------
The registry stores two families of metrics:

* **Counters.** Monotonically non-decreasing scalars, keyed by a name
  and an optional label set. Increments are non-negative (Prometheus
  semantics). Emitted as a single ``<name>{labels} <value>`` sample
  per label set.
* **Histograms.** Per-observation distributions, bucketed by a
  strictly-ascending tuple of upper bounds (plus an implicit ``+Inf``
  terminal bucket). Emitted as the Prometheus-standard triple of
  ``_bucket`` / ``_sum`` / ``_count`` lines with cumulative bucket
  counts.

Public API
----------
``registry`` : :class:`MetricsRegistry`
    The process-wide singleton. Callers SHOULD import this directly
    (``from utils.metrics import registry``) rather than constructing
    their own instance.

``MetricsRegistry`` : class
    The registry type itself, exposed for (a) tests that need a fresh
    instance and (b) future consumers that might prefer explicit
    injection over the module-level singleton.

``DEFAULT_BUCKETS`` : ``Tuple[float, ...]``
    The default histogram bucket boundaries, tuned for HTTP-request
    duration distributions measured in seconds.

``FrozenLabels`` : type alias
    ``Tuple[Tuple[str, str], ...]`` — a stable, hashable representation
    of a label set produced by :func:`_freeze_labels`. Exposed as a
    type alias so that callers that want to type-annotate low-level
    helpers (primarily tests) can do so without re-declaring the alias.

Pre-registered metrics
----------------------
The registry pre-registers the standard counters and histograms
enumerated in AAP §0.5.2.2 at construction time. Pre-registration
guarantees that ``render_prometheus()`` emits ``# HELP`` and
``# TYPE`` lines — with zero samples if no observations have been
made yet — from the very first scrape, which is a pre-condition for
dashboard panels to render sensibly on a freshly-started process.

Counters:

* ``nba_requests_total`` — HTTPS GET requests issued to the NBA Stats API.
* ``nba_request_failures_total`` — Requests that exhausted retries.
* ``nba_retries_total`` — Retry attempts made by ``tenacity``.
* ``pipeline_rows_written_total`` — Rows written to CSV artifacts.
* ``pipeline_runs_total`` — Pipeline runs completed (success or failure).
* ``games_failed_total`` — Per-game ingestion failures caught by Rule 6.

Histograms:

* ``nba_request_duration_seconds`` — NBA Stats API request durations
  (including rate-limit wait time).
* ``pipeline_duration_seconds`` — End-to-end pipeline execution
  durations per domain.

Thread safety
-------------
Every public method that mutates or reads ``self._counters`` or
``self._histograms`` acquires ``self._lock`` via ``with self._lock:``.
This includes :meth:`MetricsRegistry.inc`,
:meth:`MetricsRegistry.observe`, :meth:`MetricsRegistry.describe_counter`,
:meth:`MetricsRegistry.describe_histogram`,
:meth:`MetricsRegistry.get_counter_value`,
:meth:`MetricsRegistry.get_histogram_sum`,
:meth:`MetricsRegistry.render_prometheus`, and
:meth:`MetricsRegistry.reset`. The lock is held for the minimum
possible interval — ``render_prometheus`` snapshots under the lock
and does all string formatting outside it would be an optimisation,
but is unnecessary at this project's expected throughput and would
risk torn reads of ``sums``/``totals``/``counts`` trios, so the full
render is kept under the lock.

Scope boundaries (what this module does NOT do)
-----------------------------------------------
* It does NOT time anything itself. Callers measure durations with
  :func:`time.monotonic` and pass the result to :meth:`observe`.
  This keeps the class pure, deterministic, and trivially testable.
* It does NOT perform any I/O. There are no network listeners, no
  file writes, no subprocess invocations. The only way observations
  leave the process is via ``render_prometheus()``, which returns a
  string.
* It does NOT expose histograms with exemplars, summary percentiles,
  gauges, or untyped metrics. If those shapes become necessary, the
  right move is to depend on ``prometheus_client`` (updating the AAP
  dependency budget) rather than growing this module.
"""

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# =============================================================================
# Type aliases and module-level constants
# =============================================================================
#
# ``FrozenLabels`` is the canonical, hashable representation of a Prometheus
# label set. A Python ``dict`` is unsuitable as a dictionary key (dicts are
# unhashable by design), and ``frozenset`` would lose insertion ordering
# which harms both diffable log output and deterministic test assertions.
# A sorted tuple-of-tuples is hashable, orderable, and round-trips losslessly
# back through :func:`_freeze_labels` for the same logical label mapping.

FrozenLabels = Tuple[Tuple[str, str], ...]


# Default histogram buckets, calibrated for HTTP-request duration distributions
# measured in seconds. The values are the canonical "power-of-ten with 2.5x
# midpoints" ladder used by the prometheus_client reference library for the
# identically-named metric family. They span 5ms (fast local-cache hits) to
# 10s (NBA Stats API tail latency) with enough resolution below 1s to
# detect the ~1s rate-limit floor (Rule 2) in production traces.
#
# NOTE: mutation of this tuple is impossible (tuples are immutable). If a
# consumer needs different bucket boundaries, they pass their own sorted
# tuple to :meth:`MetricsRegistry.describe_histogram`.

DEFAULT_BUCKETS: Tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


# =============================================================================
# Label-set canonicalisation
# =============================================================================


def _freeze_labels(labels: Optional[Dict[str, str]]) -> FrozenLabels:
    """Convert a labels dict into a deterministic, hashable key.

    Label dictionaries with the same logical contents but different
    insertion orders MUST key to the same counter bucket — otherwise
    ``registry.inc("x", {"a": "1", "b": "2"})`` and
    ``registry.inc("x", {"b": "2", "a": "1"})`` would increment
    different samples, which would be a surprising correctness bug.
    Sorting alphabetically by key is the standard approach and
    matches the ``prometheus_client`` reference library's behaviour.

    All keys and values are stringified defensively. Prometheus label
    values are intrinsically strings; coercing ``int``/``bool``/``None``
    at the boundary is cheaper and safer than asking every caller to
    remember the coercion.

    Parameters
    ----------
    labels : Optional[Dict[str, str]]
        The label mapping to freeze. ``None`` or an empty dict both
        produce the empty tuple, which is the canonical "no labels"
        key.

    Returns
    -------
    FrozenLabels
        A tuple of ``(key, value)`` pairs sorted by key. The empty
        tuple represents "no labels".

    Examples
    --------
    >>> _freeze_labels(None)
    ()
    >>> _freeze_labels({})
    ()
    >>> _freeze_labels({"b": "2", "a": "1"})
    (('a', '1'), ('b', '2'))
    >>> _freeze_labels({"endpoint": "leaguedashplayerstats"})
    (('endpoint', 'leaguedashplayerstats'),)
    """
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


# =============================================================================
# Prometheus sample-value and label-string formatting
# =============================================================================
#
# The helpers below are module-level (not methods on the registry) so that
# :meth:`MetricsRegistry._render_counter` / ``_render_histogram`` can remain
# ``@staticmethod`` decorated and trivially unit-testable in isolation.


def _escape_label_value(value: str) -> str:
    """Escape a label value per the Prometheus text-format specification.

    The Prometheus exposition format requires the following three
    sequences to be escaped inside a quoted label value:

    * Backslash (``\\``) becomes ``\\\\``.
    * Double-quote (``"``) becomes ``\\"``.
    * Newline (``\\n``) becomes ``\\n``.

    The order of substitutions matters: backslash MUST be escaped
    first so that the backslashes introduced by the subsequent two
    substitutions are not themselves doubled.

    Parameters
    ----------
    value : str
        The raw label value.

    Returns
    -------
    str
        The escaped value, ready to be embedded between double quotes.

    Examples
    --------
    >>> _escape_label_value('simple')
    'simple'
    >>> _escape_label_value('has "quotes"')
    'has \\\\"quotes\\\\"'
    >>> _escape_label_value('path\\\\to\\\\thing')
    'path\\\\\\\\to\\\\\\\\thing'
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: FrozenLabels) -> str:
    """Render a frozen label tuple as ``{key="value",...}`` or the empty string.

    The Prometheus text format permits either no label block (for
    metrics without labels) or a ``{key="value",key="value"}`` block
    with comma-separated key/value pairs. This helper produces
    whichever form is appropriate given the input.

    Parameters
    ----------
    labels : FrozenLabels
        The label set in its frozen-tuple form. The empty tuple
        yields the empty string (no label block).

    Returns
    -------
    str
        The rendered label block, including the surrounding braces,
        or the empty string if ``labels`` is empty.

    Examples
    --------
    >>> _format_labels(())
    ''
    >>> _format_labels((('endpoint', 'leaguedashplayerstats'),))
    '{endpoint="leaguedashplayerstats"}'
    >>> _format_labels((('a', '1'), ('b', '2')))
    '{a="1",b="2"}'
    """
    if not labels:
        return ""
    parts = [f'{k}="{_escape_label_value(v)}"' for k, v in labels]
    return "{" + ",".join(parts) + "}"


def _merge_label_str(labels: FrozenLabels, extra: Tuple[str, str]) -> str:
    """Return ``_format_labels`` of ``labels`` plus a single extra ``(k, v)`` pair.

    This is the helper used by :meth:`MetricsRegistry._render_histogram`
    to inject the ``le="<bucket>"`` label onto each bucket sample
    without mutating the stored per-observation label set. The
    combined tuple is sorted so that the resulting output is
    deterministic regardless of which call site assembled it.

    Parameters
    ----------
    labels : FrozenLabels
        The base label set (e.g. the user-supplied labels on an
        ``observe()`` call).
    extra : Tuple[str, str]
        A single ``(key, value)`` pair to merge in.

    Returns
    -------
    str
        The rendered ``{key="value",...}`` label block containing
        both the base labels and the extra pair, alphabetically
        sorted by key.

    Examples
    --------
    >>> _merge_label_str((), ('le', '0.1'))
    '{le="0.1"}'
    >>> _merge_label_str((('endpoint', 'foo'),), ('le', '0.1'))
    '{endpoint="foo",le="0.1"}'
    """
    merged = tuple(sorted(list(labels) + [extra]))
    return _format_labels(merged)


def _format_value(value: float) -> str:
    """Render a numeric sample value in Prometheus text-format conventions.

    Prometheus accepts any IEEE-754 double-precision value, including
    the special forms ``+Inf``, ``-Inf``, and ``NaN``. In practice we
    emit:

    * Whole-number floats as bare integers. ``5.0`` renders as
      ``"5"`` rather than ``"5.0"``. Counter increments are
      canonically integer-valued (``inc(..., n=1)``), so this yields
      visibly cleaner human-readable output and matches the
      ``prometheus_client`` reference library.
    * Non-integer floats via :func:`repr`, which gives the shortest
      string that round-trips to the same float. This matches the
      behaviour of the Python standard library's ``json`` module and
      Prometheus's own reference implementation.

    Parameters
    ----------
    value : float
        The numeric value to format. May be ``int`` as well; the
        type is widened to ``float`` at the call boundary.

    Returns
    -------
    str
        The rendered sample-value string.

    Examples
    --------
    >>> _format_value(0)
    '0'
    >>> _format_value(5.0)
    '5'
    >>> _format_value(1.5)
    '1.5'
    >>> _format_value(0.025)
    '0.025'
    """
    # Booleans are a subclass of ``int`` in Python; cast through ``float``
    # so the resulting repr is numeric rather than ``"True"``/``"False"``.
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    return repr(numeric)


# =============================================================================
# Internal counter / histogram containers
# =============================================================================
#
# ``_Counter`` and ``_Histogram`` are private (leading-underscore)
# dataclasses because external code MUST go through :class:`MetricsRegistry`.
# Exposing them would invite ad-hoc manipulation that bypasses the registry's
# lock and break the thread-safety invariant.
#
# Both dataclasses are mutable (NOT ``frozen=True``) because the registry
# records observations by mutating the ``values`` / ``counts`` / ``sums`` /
# ``totals`` dictionaries on every ``inc()``/``observe()`` call. Immutable
# containers would force a copy-and-replace dance on every increment, which
# would be measurably slower and provide no safety benefit given that
# mutations already happen under the registry lock.


@dataclass
class _Counter:
    """Private container for a Prometheus counter metric.

    Attributes
    ----------
    name : str
        The metric name (e.g. ``"nba_requests_total"``).
    help_text : str
        The human-readable description emitted as the ``# HELP``
        line. May be the empty string for auto-registered counters
        that were never formally described.
    values : Dict[FrozenLabels, float]
        Per-label-set current value. Looked up by the frozen label
        tuple returned by :func:`_freeze_labels`; the empty tuple
        keys the "no labels" bucket.
    """

    name: str
    help_text: str
    values: Dict[FrozenLabels, float] = field(default_factory=dict)


@dataclass
class _Histogram:
    """Private container for a Prometheus histogram metric.

    Attributes
    ----------
    name : str
        The metric name (e.g. ``"nba_request_duration_seconds"``).
    help_text : str
        The human-readable description emitted as the ``# HELP``
        line.
    buckets : Tuple[float, ...]
        Strictly-ascending bucket upper bounds. Stored sorted at
        registration time; never mutated thereafter. The implicit
        ``+Inf`` terminal bucket is NOT included here — it is
        appended by the renderer.
    counts : Dict[FrozenLabels, List[int]]
        Per-label-set per-bucket raw counts. The list length is
        ``len(buckets) + 1``; the final slot is the ``+Inf`` count
        (every observation always increments this slot regardless of
        value).
    sums : Dict[FrozenLabels, float]
        Per-label-set running sum of observed values. Drives the
        ``_sum`` sample in the Prometheus exposition and is also the
        input to ``rate()``-based average calculations in Grafana.
    totals : Dict[FrozenLabels, int]
        Per-label-set observation count. Equivalent to
        ``counts[labels][-1]`` (the ``+Inf`` slot), but stored
        separately for O(1) access in :meth:`get_histogram_sum`-style
        helpers and for the ``_count`` sample in the exposition.
    """

    name: str
    help_text: str
    buckets: Tuple[float, ...] = DEFAULT_BUCKETS
    counts: Dict[FrozenLabels, List[int]] = field(default_factory=dict)
    sums: Dict[FrozenLabels, float] = field(default_factory=dict)
    totals: Dict[FrozenLabels, int] = field(default_factory=dict)


# =============================================================================
# MetricsRegistry
# =============================================================================


class MetricsRegistry:
    """Thread-safe in-process metric registry.

    Instantiated once at module load as :data:`registry`. The standard
    counters and histograms enumerated in AAP §0.5.2.2 are
    pre-registered in :meth:`_register_defaults` so that
    :meth:`render_prometheus` emits ``# HELP`` / ``# TYPE`` lines even
    when no observations have yet been made — a pre-condition for
    dashboards that rely on metric *presence* to render panels.

    All public mutators and readers acquire the internal
    :class:`threading.Lock` via ``with self._lock:``. The lock is held
    only for the critical section of each method; long-running work
    (formatting, sorting) is serialised through the lock too because
    the throughput at this project's scale (< 1 request/sec by
    Rule 2) does not justify the complexity of a lock-free snapshot
    implementation.

    Examples
    --------
    Typical call sites:

    >>> from utils.metrics import registry
    >>> registry.inc("nba_requests_total", {"endpoint": "leaguedashplayerstats"})
    >>> registry.observe("nba_request_duration_seconds", 0.42,
    ...                  {"endpoint": "leaguedashplayerstats"})
    >>> print(registry.render_prometheus())  # doctest: +SKIP
    """

    def __init__(self) -> None:
        # Lock FIRST so subsequent helper calls that require it (via
        # ``describe_counter``/``describe_histogram`` in
        # ``_register_defaults``) find it already initialised.
        self._lock: threading.Lock = threading.Lock()
        self._counters: Dict[str, _Counter] = {}
        self._histograms: Dict[str, _Histogram] = {}
        self._register_defaults()

    # -------------------------------------------------------------------------
    # Pre-registration
    # -------------------------------------------------------------------------

    def _register_defaults(self) -> None:
        """Pre-register the metrics enumerated in AAP §0.5.2.2.

        This is invoked exactly once from :meth:`__init__`. Callers
        MUST NOT invoke it again — re-registration would be a no-op
        in terms of stored values (see
        :meth:`describe_counter`/:meth:`describe_histogram` idempotency
        semantics) but would silently overwrite user-supplied help
        text, which is surprising.
        """
        # ---- Counters ----
        self.describe_counter(
            "nba_requests_total",
            "Total number of HTTPS GET requests issued to the NBA Stats API.",
        )
        self.describe_counter(
            "nba_request_failures_total",
            "Total number of NBA Stats API requests that exhausted retries.",
        )
        self.describe_counter(
            "nba_retries_total",
            "Total number of retry attempts made by tenacity against the NBA Stats API.",
        )
        self.describe_counter(
            "pipeline_rows_written_total",
            "Total number of rows written to CSV artifacts across all pipelines.",
        )
        self.describe_counter(
            "pipeline_runs_total",
            "Total number of pipeline runs completed (success or failure).",
        )
        self.describe_counter(
            "games_failed_total",
            "Total number of per-game ingestion failures caught by Rule 6 fail-safe iteration.",
        )

        # ---- Histograms ----
        self.describe_histogram(
            "nba_request_duration_seconds",
            "Duration of NBA Stats API requests including rate-limit wait time.",
        )
        self.describe_histogram(
            "pipeline_duration_seconds",
            "Duration of end-to-end pipeline execution per domain.",
        )

    # -------------------------------------------------------------------------
    # Name validation
    # -------------------------------------------------------------------------

    @staticmethod
    def _validate_name(name: str) -> None:
        """Validate a metric name against the Prometheus naming rules.

        Per the Prometheus exposition format, metric names MUST match
        the regex ``[a-zA-Z_:][a-zA-Z0-9_:]*``. We enforce a slightly
        stricter variant (only ASCII alphanumerics plus ``_`` and
        ``:``) because the built-in string methods we use for the
        check (``str.isalpha``/``str.isalnum``) accept Unicode
        letters, which Prometheus does not.

        Parameters
        ----------
        name : str
            The candidate metric name.

        Raises
        ------
        ValueError
            If ``name`` is not a non-empty string, or if it contains
            characters outside ``[A-Za-z0-9_:]``, or if the first
            character is not a letter or underscore.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("metric name must be a non-empty string")

        # Prometheus naming: [a-zA-Z_:][a-zA-Z0-9_:]*
        #
        # The first-character check is slightly more permissive than the
        # spec's ``:`` because many community tools (including
        # grafana-agent) reject metrics that START with ``:`` even
        # though the Prometheus spec technically allows it. We follow
        # the stricter community convention here — metric names SHOULD
        # start with a letter or underscore, never with a colon.
        first = name[0]
        if not (first.isalpha() and first.isascii()) and first != "_":
            raise ValueError(
                f"metric name '{name}' must start with a letter or underscore"
            )

        # Subsequent characters: ASCII alphanumeric, underscore, or colon.
        # ``str.isalnum`` returns True for Unicode letters/digits which is
        # why we also check ``isascii``.
        for ch in name:
            if ch in ("_", ":"):
                continue
            if ch.isalnum() and ch.isascii():
                continue
            raise ValueError(
                f"metric name '{name}' may only contain ASCII alphanumerics, '_', or ':'"
            )

    # -------------------------------------------------------------------------
    # Describe (idempotent registration)
    # -------------------------------------------------------------------------

    def describe_counter(self, name: str, help_text: str) -> None:
        """Register (or re-describe) a counter metric.

        This method is idempotent: calling it with a name that is
        already registered as a counter simply updates the help text.
        Calling it with a name that is already registered as a
        histogram raises :class:`ValueError` — metric types are
        immutable once set, matching the Prometheus convention.

        Parameters
        ----------
        name : str
            The metric name. Validated against the Prometheus naming
            rules by :meth:`_validate_name`.
        help_text : str
            The human-readable description emitted on the ``# HELP``
            line.

        Raises
        ------
        ValueError
            If ``name`` violates the Prometheus naming rules or if
            it is already registered as a histogram.
        """
        self._validate_name(name)
        with self._lock:
            if name in self._histograms:
                raise ValueError(
                    f"Metric name '{name}' is already registered as a histogram"
                )
            if name in self._counters:
                # Re-description: update help text, preserve values.
                self._counters[name].help_text = help_text
            else:
                self._counters[name] = _Counter(name=name, help_text=help_text)

    def describe_histogram(
        self,
        name: str,
        help_text: str,
        buckets: Tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> None:
        """Register (or re-describe) a histogram metric.

        Like :meth:`describe_counter`, this method is idempotent with
        respect to help text. The ``buckets`` argument is applied
        ONLY on first registration — subsequent calls leave the
        existing bucket layout untouched, because rewriting the
        bucket layout mid-process would silently invalidate the
        count arrays for every existing per-label-set entry.

        Parameters
        ----------
        name : str
            The metric name.
        help_text : str
            The human-readable description.
        buckets : Tuple[float, ...]
            The strictly-ascending bucket upper bounds. Stored sorted
            so that operator-supplied out-of-order buckets are
            silently corrected rather than producing rendering bugs.
            Defaults to :data:`DEFAULT_BUCKETS`.

        Raises
        ------
        ValueError
            If ``name`` violates the Prometheus naming rules or if
            it is already registered as a counter.
        """
        self._validate_name(name)
        with self._lock:
            if name in self._counters:
                raise ValueError(
                    f"Metric name '{name}' is already registered as a counter"
                )
            if name in self._histograms:
                # Re-description: update help text only. Buckets are
                # preserved so that previously-stored count arrays
                # remain aligned with the bucket boundaries.
                self._histograms[name].help_text = help_text
            else:
                self._histograms[name] = _Histogram(
                    name=name,
                    help_text=help_text,
                    buckets=tuple(sorted(buckets)),
                )

    # -------------------------------------------------------------------------
    # Observation recording
    # -------------------------------------------------------------------------

    def inc(
        self,
        name: str,
        labels: Optional[Dict[str, str]] = None,
        n: float = 1.0,
    ) -> None:
        """Increment a counter by ``n`` (default ``1.0``).

        If the counter has not been previously described via
        :meth:`describe_counter`, it is auto-registered with an empty
        help text. This keeps call sites ergonomic — modules can call
        ``registry.inc("some_counter")`` without a paired
        ``describe_counter`` call — at the cost of the ``# HELP`` line
        being blank for that metric.

        Parameters
        ----------
        name : str
            The counter name. Auto-registered if unknown.
        labels : Optional[Dict[str, str]]
            Optional label mapping. Frozen into a sorted tuple by
            :func:`_freeze_labels` so that insertion-order-different
            dicts with the same logical content key to the same
            counter bucket.
        n : float, default ``1.0``
            The increment amount. MUST be non-negative (Prometheus
            counter semantics). Typical values are ``1``, ``1.0``, or
            the number of rows just written to a CSV artifact.

        Raises
        ------
        ValueError
            If ``n < 0`` (counters are monotonically non-decreasing)
            or if ``name`` is already registered as a histogram.
        """
        if n < 0:
            raise ValueError(
                "counter increments must be non-negative (Prometheus semantics)"
            )
        key = _freeze_labels(labels)
        with self._lock:
            counter = self._counters.get(name)
            if counter is None:
                # Before auto-registering, guard against a same-name
                # collision with an existing histogram. We do NOT
                # re-invoke ``describe_counter`` here because doing so
                # would require re-acquiring the lock (it would
                # deadlock on the non-reentrant :class:`threading.Lock`).
                if name in self._histograms:
                    raise ValueError(
                        f"Metric name '{name}' is registered as a histogram, not a counter"
                    )
                counter = _Counter(name=name, help_text="")
                self._counters[name] = counter
            counter.values[key] = counter.values.get(key, 0.0) + float(n)

    def observe(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Record a single observation in a histogram.

        Each observation is added to every bucket whose upper bound
        is greater than or equal to ``value``, plus the implicit
        ``+Inf`` bucket. The sum and count totals are also updated.
        If the histogram has not been previously described via
        :meth:`describe_histogram`, it is auto-registered with the
        default bucket layout and empty help text.

        Parameters
        ----------
        name : str
            The histogram name. Auto-registered if unknown.
        value : float
            The observed value. May be negative (e.g. for
            queue-age-from-deadline metrics); negative observations
            are recorded normally and fall into buckets per the
            ``value <= upper`` rule.
        labels : Optional[Dict[str, str]]
            Optional label mapping, same semantics as :meth:`inc`.

        Raises
        ------
        ValueError
            If ``name`` is already registered as a counter.
        """
        key = _freeze_labels(labels)
        with self._lock:
            hist = self._histograms.get(name)
            if hist is None:
                if name in self._counters:
                    raise ValueError(
                        f"Metric name '{name}' is registered as a counter, not a histogram"
                    )
                hist = _Histogram(name=name, help_text="")
                self._histograms[name] = hist
            counts = hist.counts.get(key)
            if counts is None:
                # +1 slot for the implicit ``+Inf`` bucket at the end.
                counts = [0] * (len(hist.buckets) + 1)
                hist.counts[key] = counts
            # Per-bucket RAW counts (not cumulative). ``counts[idx]``
            # stores the number of observations whose value falls into
            # the half-open range ``(buckets[idx-1], buckets[idx]]``
            # (or ``(-inf, buckets[0]]`` for idx == 0). The implicit
            # ``+Inf`` bucket at ``counts[-1]`` stores only observations
            # that exceed all explicit buckets. Cumulative counts --
            # which are what Prometheus emits on the wire -- are
            # computed on the fly by :meth:`_render_histogram` via a
            # running sum across ``counts``. Storing raw counts here
            # keeps the hot path O(buckets) worst case (short-circuits
            # on the first containing bucket via ``break``) and avoids
            # re-balancing every slot on every observation.
            for idx, upper in enumerate(hist.buckets):
                if value <= upper:
                    counts[idx] += 1
                    break
            else:
                # Value exceeds every explicit bucket -> ``+Inf``.
                counts[-1] += 1
            hist.sums[key] = hist.sums.get(key, 0.0) + float(value)
            hist.totals[key] = hist.totals.get(key, 0) + 1

    # -------------------------------------------------------------------------
    # Query helpers (primarily for tests and diagnostics)
    # -------------------------------------------------------------------------

    def get_counter_value(
        self,
        name: str,
        labels: Optional[Dict[str, str]] = None,
    ) -> float:
        """Return the current value of a counter for a given label set.

        Returns ``0.0`` when the counter has never been registered or
        when it has been registered but never incremented for the
        given label set. This matches the Prometheus convention that
        unobserved metrics are zero-valued rather than undefined.

        Parameters
        ----------
        name : str
            The counter name.
        labels : Optional[Dict[str, str]]
            The label set to look up. ``None`` / empty dict means
            "no labels".

        Returns
        -------
        float
            The current value. Always a Python ``float``, even when
            the stored increments happen to sum to an integer.
        """
        key = _freeze_labels(labels)
        with self._lock:
            counter = self._counters.get(name)
            if counter is None:
                return 0.0
            return counter.values.get(key, 0.0)

    def get_histogram_sum(
        self,
        name: str,
        labels: Optional[Dict[str, str]] = None,
    ) -> float:
        """Return the running sum of a histogram for a given label set.

        Returns ``0.0`` when the histogram has never been registered
        or when no observations have been recorded for the given
        label set.

        Parameters
        ----------
        name : str
            The histogram name.
        labels : Optional[Dict[str, str]]
            The label set to look up.

        Returns
        -------
        float
            The running sum.
        """
        key = _freeze_labels(labels)
        with self._lock:
            hist = self._histograms.get(name)
            if hist is None:
                return 0.0
            return hist.sums.get(key, 0.0)

    # -------------------------------------------------------------------------
    # Reset (tests only)
    # -------------------------------------------------------------------------

    def reset(self) -> None:
        """Clear all recorded observations while preserving registrations.

        Intended for tests that run against the module-level singleton
        — without a reset hook, test ordering would leak state across
        cases and make failures non-reproducible. Production code
        MUST NOT call this: it would zero the run counter and the
        rolling request totals mid-pipeline, corrupting the dashboard
        that depends on them.

        Notes
        -----
        The registrations (name, help text, bucket layout) are
        preserved — only the stored observations are cleared. This
        way, post-reset ``render_prometheus()`` output still emits
        ``# HELP`` / ``# TYPE`` lines for every pre-registered metric,
        matching the first-boot state.
        """
        with self._lock:
            for counter in self._counters.values():
                counter.values.clear()
            for hist in self._histograms.values():
                hist.counts.clear()
                hist.sums.clear()
                hist.totals.clear()

    # -------------------------------------------------------------------------
    # Prometheus exposition
    # -------------------------------------------------------------------------

    def render_prometheus(self) -> str:
        """Render the Prometheus text-format exposition of the registry.

        The output format conforms to the Prometheus exposition
        specification version 0.0.4:

        * Each metric family begins with a ``# HELP <name> <help>``
          line and a ``# TYPE <name> <counter|histogram>`` line.
        * Counters emit a single ``<name>{labels} <value>`` sample
          per observed label set, plus a single zero sample with no
          labels for pre-registered-but-unobserved counters.
        * Histograms emit, per observed label set, a series of
          cumulative ``<name>_bucket{le="<bound>",...} <count>``
          samples (one per bucket, plus the ``+Inf`` terminal
          bucket), a ``<name>_sum{...} <sum>`` sample, and a
          ``<name>_count{...} <total>`` sample.
        * Pre-registered-but-unobserved histograms emit a single
          empty-label series with all buckets at zero.
        * The overall output ends with a trailing newline, as
          required by the Prometheus specification.

        Metric families are sorted alphabetically by name (counters
        first, then histograms) to give deterministic output that is
        easy to diff between runs.

        Returns
        -------
        str
            The exposition text, ready to be printed on stdout by
            ``run.py metrics`` or returned as the body of an HTTP
            ``/metrics`` response if one is ever added.
        """
        lines: List[str] = []
        with self._lock:
            for name in sorted(self._counters.keys()):
                lines.extend(self._render_counter(self._counters[name]))
            for name in sorted(self._histograms.keys()):
                lines.extend(self._render_histogram(self._histograms[name]))
        # Prometheus requires a trailing newline. ``"\n".join`` does NOT
        # produce one; we append it explicitly.
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_counter(counter: _Counter) -> List[str]:
        """Render a single counter metric family as a list of lines.

        The returned list is guaranteed to contain at least three
        entries: the ``# HELP`` line, the ``# TYPE`` line, and one
        sample line (either an empty-labels zero sample for
        pre-registered-but-unobserved counters, or one sample per
        observed label set sorted by the frozen label tuple).

        Parameters
        ----------
        counter : _Counter
            The internal counter container.

        Returns
        -------
        List[str]
            The rendered lines in order.
        """
        lines = [
            f"# HELP {counter.name} {counter.help_text}",
            f"# TYPE {counter.name} counter",
        ]
        if not counter.values:
            # Emit a single zero sample with no labels so that scrapers
            # always observe the metric's existence, even before the
            # first increment. Prometheus tooling relies on a metric
            # being ``present`` to light up alert rules that use
            # ``absent()`` or ``rate()``.
            lines.append(f"{counter.name} 0")
            return lines
        for key in sorted(counter.values.keys()):
            value = counter.values[key]
            label_str = _format_labels(key)
            lines.append(f"{counter.name}{label_str} {_format_value(value)}")
        return lines

    @staticmethod
    def _render_histogram(hist: _Histogram) -> List[str]:
        """Render a single histogram metric family as a list of lines.

        Each observed label set produces:

        * One ``<name>_bucket{le="<bound>",...} <cum>`` line per
          configured bucket (cumulative count of observations with
          ``value <= bound``), followed by a ``le="+Inf"`` line.
        * One ``<name>_sum{...} <sum>`` line.
        * One ``<name>_count{...} <total>`` line.

        Pre-registered-but-unobserved histograms emit a single
        empty-labels series with every bucket at zero, so that
        dashboards can detect the metric even before the first
        observation.

        Parameters
        ----------
        hist : _Histogram
            The internal histogram container.

        Returns
        -------
        List[str]
            The rendered lines in order.
        """
        lines = [
            f"# HELP {hist.name} {hist.help_text}",
            f"# TYPE {hist.name} histogram",
        ]
        if not hist.counts:
            # Empty histogram: emit a single zero-valued series so the
            # metric name is scrapable immediately after boot. The
            # bucket labels use the same numeric formatting as
            # populated series so diffs across runs remain clean.
            for upper in hist.buckets:
                lines.append(
                    f'{hist.name}_bucket{{le="{_format_value(upper)}"}} 0'
                )
            lines.append(f'{hist.name}_bucket{{le="+Inf"}} 0')
            lines.append(f"{hist.name}_sum 0")
            lines.append(f"{hist.name}_count 0")
            return lines

        for key in sorted(hist.counts.keys()):
            counts = hist.counts[key]
            cumulative = 0
            # Prometheus histograms are cumulative: bucket{le="0.1"}
            # counts EVERY observation with value <= 0.1, not just
            # those between the previous bucket boundary and 0.1.
            # We store raw per-bucket counts (see :meth:`observe`)
            # and convert to cumulative here at render time.
            for idx, upper in enumerate(hist.buckets):
                cumulative += counts[idx]
                label_inner = _merge_label_str(key, ("le", _format_value(upper)))
                lines.append(f"{hist.name}_bucket{label_inner} {cumulative}")
            # The ``+Inf`` slot is counted separately; every observation
            # increments it unconditionally (see :meth:`observe`), which
            # means adding ``counts[-1]`` to the running cumulative is
            # the total observation count — equivalent to
            # ``hist.totals[key]`` but computed from the count list so
            # the invariant is self-consistent.
            cumulative += counts[-1]
            label_inner = _merge_label_str(key, ("le", "+Inf"))
            lines.append(f"{hist.name}_bucket{label_inner} {cumulative}")
            # ``_sum`` and ``_count`` samples are labeled with the
            # base label set only (no ``le`` label, unlike the bucket
            # samples). ``_format_labels`` gracefully renders an
            # empty tuple as the empty string.
            lines.append(
                f"{hist.name}_sum{_format_labels(key)} "
                f"{_format_value(hist.sums.get(key, 0.0))}"
            )
            lines.append(
                f"{hist.name}_count{_format_labels(key)} "
                f"{hist.totals.get(key, 0)}"
            )
        return lines


# =============================================================================
# Module-level singleton
# =============================================================================
#
# Every consumer of this module is expected to import ``registry`` and use it
# directly. The singleton pattern is used for three reasons:
#
#   1. The Prometheus scraping model assumes a process-wide registry —
#      multiple registries would fragment the exposition and break scrape
#      endpoints that discover metrics by name.
#   2. Passing a registry instance through every call site would pollute
#      every public signature (``NBAClient.__init__``, every pipeline's
#      ``run()`` signature, the CLI entry point) for no real benefit.
#   3. Thread safety is guaranteed by the internal lock, so the global
#      mutability hazard normally associated with module-level singletons
#      does not apply here.
#
# Tests that want isolation can either call :meth:`MetricsRegistry.reset`
# or construct their own ``MetricsRegistry()`` instance.

registry: MetricsRegistry = MetricsRegistry()
