"""Unit tests for :mod:`utils.metrics`.

Verifies counter and histogram registration, Prometheus text-format
rendering, label canonicalization, escape sequences, thread safety, and
the module-level singleton's pre-registered metric catalog. Satisfies
the project-level Observability rule (AAP §0.7.3.1) which requires a
locally-exercisable metrics endpoint.

Design notes:

* Most tests construct a fresh :class:`utils.metrics.MetricsRegistry`
  instance to decouple their behavior from the autouse reset fixture
  declared in :mod:`tests.conftest` and to prevent test-ordering
  dependencies. Only the Phase 2.1 pre-registration tests use the
  module-level :data:`utils.metrics.registry` singleton directly.
* Assertions on rendered Prometheus output prefer ``line in
  output.splitlines()`` over substring membership checks because the
  rendered payload contains many pre-registered metrics and a
  substring match like ``"c 3"`` could otherwise spuriously match a
  line such as ``"c 30"``.
* Histogram assertions account for cumulative bucket semantics: every
  ``le=`` bucket whose upper bound is greater than or equal to the
  observed value has a cumulative count one greater than the previous.
"""

from __future__ import annotations

import re
import threading
from typing import Dict, List, Tuple

import pytest

from utils import metrics as metrics_module
from utils.metrics import DEFAULT_BUCKETS, MetricsRegistry, registry


# ---------------------------------------------------------------------------
# Phase 2.1 — Pre-registered names on the module singleton
# ---------------------------------------------------------------------------


def test_pre_registered_counters_exist() -> None:
    """The module-level registry pre-registers the six operational counters.

    Verifies that every counter named in AAP §0.5.2.2 is known to the
    registry by observing that :meth:`MetricsRegistry.get_counter_value`
    returns ``0.0`` for each one (the default post-reset value). This
    test relies on the autouse ``_reset_metrics_registry_between_tests``
    fixture to guarantee the pristine state.
    """
    expected_counters = (
        "nba_requests_total",
        "nba_request_failures_total",
        "nba_retries_total",
        "pipeline_rows_written_total",
        "pipeline_runs_total",
        "games_failed_total",
    )
    for name in expected_counters:
        assert registry.get_counter_value(name, None) == 0.0, (
            f"Pre-registered counter '{name}' did not read as 0.0"
        )


def test_pre_registered_histograms_exist() -> None:
    """The module-level registry pre-registers two latency histograms.

    Observing a small positive value on each pre-registered histogram
    must not raise and must update the histogram's running sum.
    """
    registry.observe("nba_request_duration_seconds", 0.01)
    registry.observe("pipeline_duration_seconds", 0.01)
    assert abs(registry.get_histogram_sum("nba_request_duration_seconds", None) - 0.01) < 1e-9
    assert abs(registry.get_histogram_sum("pipeline_duration_seconds", None) - 0.01) < 1e-9


def test_registry_is_metricsregistry_instance() -> None:
    """The module-level ``registry`` singleton is a :class:`MetricsRegistry`."""
    assert isinstance(registry, MetricsRegistry)


def test_default_buckets_is_sorted_tuple() -> None:
    """``DEFAULT_BUCKETS`` is an 11-element strictly-increasing float tuple.

    Matches the canonical Prometheus-client default:
    ``(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)``.
    """
    assert isinstance(DEFAULT_BUCKETS, tuple)
    assert len(DEFAULT_BUCKETS) == 11
    assert all(isinstance(bound, float) for bound in DEFAULT_BUCKETS)
    for previous, current in zip(DEFAULT_BUCKETS, DEFAULT_BUCKETS[1:]):
        assert previous < current, (
            f"DEFAULT_BUCKETS not strictly increasing: {previous} !< {current}"
        )
    expected: Tuple[float, ...] = (
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
    )
    assert DEFAULT_BUCKETS == expected


def test_reset_between_tests_is_applied_via_autouse_fixture() -> None:
    """The conftest autouse fixture resets the singleton before every test.

    If the fixture were not active, a prior test that incremented
    ``nba_requests_total`` would cause this test to observe a non-zero
    value. Because the autouse fixture resets between tests, this
    assertion must hold regardless of test ordering.
    """
    assert registry.get_counter_value("nba_requests_total", None) == 0.0


# ---------------------------------------------------------------------------
# Phase 2.2 — Counter increment basics
# ---------------------------------------------------------------------------


def test_inc_default_n_is_one() -> None:
    """``inc`` without an explicit ``n`` increments by exactly 1.0."""
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    local_registry.inc("c", {"k": "v"})
    assert local_registry.get_counter_value("c", {"k": "v"}) == 1.0


def test_inc_with_fractional_n() -> None:
    """``inc`` accepts and accumulates fractional increments."""
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    local_registry.inc("c", {"k": "v"}, n=2.5)
    assert local_registry.get_counter_value("c", {"k": "v"}) == 2.5


def test_inc_accumulates_across_calls() -> None:
    """Successive ``inc`` calls add to the existing counter value."""
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    local_registry.inc("c", {"k": "v"})
    local_registry.inc("c", {"k": "v"})
    local_registry.inc("c", {"k": "v"})
    assert local_registry.get_counter_value("c", {"k": "v"}) == 3.0


def test_inc_negative_raises_valueerror() -> None:
    """Negative increments are rejected per Prometheus counter semantics.

    Counters must be monotonically non-decreasing, so passing ``n=-1``
    must raise :class:`ValueError` with a message that mentions the
    non-negative constraint.
    """
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    with pytest.raises(ValueError) as excinfo:
        local_registry.inc("c", {"k": "v"}, n=-1)
    assert "non-negative" in str(excinfo.value).lower()


def test_inc_zero_is_allowed_but_nonmutating() -> None:
    """``inc(n=0)`` must not raise and must not advance the counter value.

    Passing ``n=0`` does register the label set in the counter's values
    dictionary (so it renders as ``c{labels} 0``) but it does not mutate
    any previously-accumulated value.
    """
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    local_registry.inc("c", {"k": "v"}, n=5)
    local_registry.inc("c", {"k": "v"}, n=0)
    assert local_registry.get_counter_value("c", {"k": "v"}) == 5.0


def test_inc_without_labels_uses_empty_label_set() -> None:
    """``inc`` and ``get_counter_value`` treat ``None`` as the empty label set."""
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    local_registry.inc("c")
    assert local_registry.get_counter_value("c") == 1.0
    assert local_registry.get_counter_value("c", None) == 1.0
    assert local_registry.get_counter_value("c", {}) == 1.0


def test_get_counter_value_returns_zero_for_unseen_labels() -> None:
    """Unseen label sets return ``0.0`` rather than raising :class:`KeyError`."""
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    assert local_registry.get_counter_value("c", {"other": "labels"}) == 0.0


def test_auto_registration_on_inc() -> None:
    """Calling ``inc`` on an unknown counter name auto-registers it.

    The registry must accept ``inc`` on a name that was never declared
    via :meth:`MetricsRegistry.describe_counter` and produce a valid
    Prometheus data line in the rendered output.
    """
    local_registry = MetricsRegistry()
    local_registry.inc("new_counter", {"k": "v"})
    output = local_registry.render_prometheus()
    assert 'new_counter{k="v"} 1' in output.splitlines()


# ---------------------------------------------------------------------------
# Phase 2.3 — Label canonicalization
# ---------------------------------------------------------------------------


def test_label_canonicalization_key_order_irrelevant() -> None:
    """Labels are canonicalized by sorted key so input key-order is ignored.

    Two calls that pass the same labels in different dict orders must
    target the same internal counter bucket.
    """
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")

    labels_a: Dict[str, str] = {"b": "2", "a": "1"}
    labels_b: Dict[str, str] = {"a": "1", "b": "2"}

    local_registry.inc("c", labels_a)
    local_registry.inc("c", labels_b)

    assert local_registry.get_counter_value("c", {"a": "1", "b": "2"}) == 2.0
    assert local_registry.get_counter_value("c", {"b": "2", "a": "1"}) == 2.0


def test_label_values_coerced_to_strings() -> None:
    """Non-string label values are coerced to their :func:`str` representation.

    Passing an integer value such as ``8080`` must be stored equivalently
    to the string ``"8080"`` so downstream lookups work regardless of
    whether the caller types the port as an int or a string literal.
    """
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    local_registry.inc("c", {"port": 8080})
    assert local_registry.get_counter_value("c", {"port": "8080"}) == 1.0


# ---------------------------------------------------------------------------
# Phase 2.4 — Label value escaping in Prometheus output
# ---------------------------------------------------------------------------


def test_label_value_escapes_backslash_quote_newline() -> None:
    """Backslash, double-quote, and newline are escaped in rendered output.

    Per the Prometheus text exposition format, label values are quoted
    strings whose content uses the escape sequences ``\\\\``, ``\\"``,
    and ``\\n``. The production helper :func:`_escape_label_value`
    applies backslash escaping first so subsequent escapes are not
    double-escaped by the backslash pass.
    """
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    local_registry.inc("c", {"k": 'a"b\\c\nd'})
    output = local_registry.render_prometheus()
    # Python string literal 'k="a\\"b\\\\c\\nd"' represents the characters
    # k="a\"b\\c\nd" which is exactly the escape sequence we expect.
    expected_fragment = 'k="a\\"b\\\\c\\nd"'
    assert expected_fragment in output


def test_label_value_without_special_chars_unescaped() -> None:
    """Label values without escape-worthy characters appear verbatim."""
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    local_registry.inc("c", {"endpoint": "leaguedashplayerstats"})
    output = local_registry.render_prometheus()
    assert 'endpoint="leaguedashplayerstats"' in output
    # Confirm no escape characters leaked into the output for this value.
    assert 'endpoint="leaguedashplayerstats\\"' not in output
    assert 'endpoint="\\\\leaguedashplayerstats' not in output


# ---------------------------------------------------------------------------
# Phase 2.5 — Histogram semantics
# ---------------------------------------------------------------------------


def test_observe_updates_sum_and_count() -> None:
    """``observe`` accumulates into the histogram's sum and count vectors."""
    local_registry = MetricsRegistry()
    local_registry.describe_histogram("h", "help")
    local_registry.observe("h", 0.05)
    local_registry.observe("h", 0.10)
    assert abs(local_registry.get_histogram_sum("h", None) - 0.15) < 1e-9


def test_observe_increments_all_buckets_at_or_above_value_cumulative_rendering() -> None:
    """Rendered bucket counts follow Prometheus cumulative semantics.

    After observing ``0.05``, every ``le=`` bucket whose upper bound
    is greater than or equal to ``0.05`` shows a count of 1, and every
    bucket with an upper bound strictly less than ``0.05`` shows 0.
    The ``le="+Inf"`` bucket must always equal the total number of
    observations (here, 1).
    """
    local_registry = MetricsRegistry()
    local_registry.describe_histogram("h", "help")
    local_registry.observe("h", 0.05)
    output = local_registry.render_prometheus()
    bucket_lines: List[str] = [
        line for line in output.splitlines() if line.startswith("h_bucket")
    ]
    # Parse each "h_bucket{le=\"X\"} N" line and extract (le, count).
    pattern = re.compile(r'h_bucket\{le="([^"]+)"\}\s+(\d+)$')
    parsed: List[Tuple[str, int]] = []
    for line in bucket_lines:
        match = pattern.match(line)
        assert match is not None, f"Unparseable bucket line: {line!r}"
        parsed.append((match.group(1), int(match.group(2))))
    assert parsed, "No h_bucket lines were rendered"
    # The +Inf bucket must always be present and equal total observations.
    inf_entries = [count for le, count in parsed if le == "+Inf"]
    assert inf_entries == [1]
    # Every finite bucket whose numeric upper bound is >= 0.05 must be 1.
    for le, count in parsed:
        if le == "+Inf":
            continue
        upper_bound = float(le)
        if upper_bound >= 0.05:
            assert count == 1, f"bucket le={le!r} count {count} (expected 1)"
        else:
            assert count == 0, f"bucket le={le!r} count {count} (expected 0)"


def test_observe_auto_registers_with_default_buckets() -> None:
    """Calling ``observe`` on an unknown histogram auto-registers it.

    The auto-registration uses :data:`DEFAULT_BUCKETS`, so the rendered
    output must include the ``+Inf`` bucket (the sentinel upper bound
    that always exists in a Prometheus histogram).
    """
    local_registry = MetricsRegistry()
    local_registry.observe("hnew", 0.5)
    output = local_registry.render_prometheus()
    assert 'hnew_bucket{le="+Inf"} 1' in output.splitlines()


def test_observe_with_labels_records_per_label_histogram() -> None:
    """Each distinct label set maintains an independent histogram vector."""
    local_registry = MetricsRegistry()
    local_registry.describe_histogram("h", "help")
    local_registry.observe("h", 0.05, {"endpoint": "x"})
    local_registry.observe("h", 0.10, {"endpoint": "y"})

    # Independent sums per label set.
    assert abs(local_registry.get_histogram_sum("h", {"endpoint": "x"}) - 0.05) < 1e-9
    assert abs(local_registry.get_histogram_sum("h", {"endpoint": "y"}) - 0.10) < 1e-9
    # Unseen label sets report zero sum rather than raising.
    assert local_registry.get_histogram_sum("h", {"endpoint": "z"}) == 0.0
    # The empty-label bucket is also an independent vector.
    assert local_registry.get_histogram_sum("h", None) == 0.0


def test_describe_histogram_stores_buckets_sorted() -> None:
    """``describe_histogram`` stores its ``buckets`` argument in sorted order.

    Passing ``[1.0, 0.1, 0.5]`` must result in rendered bucket order
    ``le="0.1"`` → ``le="0.5"`` → ``le="1"`` → ``le="+Inf"``. Note that
    the production :func:`_format_value` helper renders whole-number
    floats without the trailing ``.0`` (so ``1.0`` renders as ``"1"``).
    """
    local_registry = MetricsRegistry()
    local_registry.describe_histogram("h", "help", buckets=[1.0, 0.1, 0.5])
    local_registry.observe("h", 0.2)
    output = local_registry.render_prometheus()
    idx_small = output.index('le="0.1"')
    idx_mid = output.index('le="0.5"')
    idx_big = output.index('le="1"')
    idx_inf = output.index('le="+Inf"')
    assert idx_small < idx_mid < idx_big < idx_inf


def test_observe_negative_value_does_not_raise() -> None:
    """Negative histogram observations are permitted per Prometheus convention.

    The Prometheus data model does not forbid negative histogram
    observations; the production module accepts them, stores them, and
    counts them in every bucket (because the observation is less than
    every finite upper bound).
    """
    local_registry = MetricsRegistry()
    local_registry.describe_histogram("h", "help")
    local_registry.observe("h", -0.05)
    assert abs(local_registry.get_histogram_sum("h", None) - (-0.05)) < 1e-9
    output = local_registry.render_prometheus()
    # Every finite bucket upper bound is greater than -0.05 so every
    # bucket count is 1, including the smallest default bucket.
    assert 'h_bucket{le="0.005"} 1' in output.splitlines()
    assert 'h_bucket{le="+Inf"} 1' in output.splitlines()


# ---------------------------------------------------------------------------
# Phase 2.6 — Type collisions between counters and histograms
# ---------------------------------------------------------------------------


def test_inc_on_histogram_name_raises_valueerror() -> None:
    """Calling ``inc`` on a name registered as a histogram raises ValueError."""
    local_registry = MetricsRegistry()
    local_registry.describe_histogram("h", "help")
    with pytest.raises(ValueError) as excinfo:
        local_registry.inc("h", {"k": "v"})
    message = str(excinfo.value)
    assert "histogram" in message
    assert "counter" in message


def test_observe_on_counter_name_raises_valueerror() -> None:
    """Calling ``observe`` on a name registered as a counter raises ValueError."""
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    with pytest.raises(ValueError) as excinfo:
        local_registry.observe("c", 1.0)
    message = str(excinfo.value)
    assert "counter" in message
    assert "histogram" in message


def test_describe_counter_twice_is_idempotent() -> None:
    """Re-describing an existing counter of the same type must not raise.

    A counter declared at import time from multiple module sites should
    produce identical effects; describing the same name as a histogram,
    however, is an incompatible contract change and must raise.
    """
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    # Same help text, same kind — must be idempotent.
    local_registry.describe_counter("c", "help")

    # Re-describing as a histogram is a type collision.
    with pytest.raises(ValueError) as excinfo:
        local_registry.describe_histogram("c", "help")
    assert "counter" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Phase 2.7 — Metric name validation (Prometheus grammar)
# ---------------------------------------------------------------------------


def test_inc_rejects_invalid_metric_name_starting_with_digit() -> None:
    """Metric names must not begin with a digit (Prometheus grammar).

    The Prometheus metric name grammar is ``[a-zA-Z_:][a-zA-Z0-9_:]*``;
    a leading digit is invalid. The registry rejects such names with a
    :class:`ValueError` whose message hints at the naming rule.
    """
    local_registry = MetricsRegistry()
    with pytest.raises(ValueError) as excinfo:
        local_registry.inc("1bad_name", None, 1)
    message = str(excinfo.value)
    assert "1bad_name" in message
    assert ("letter" in message) or ("underscore" in message)


def test_inc_rejects_invalid_metric_name_with_hyphen() -> None:
    """Hyphens are not in the Prometheus metric-name grammar."""
    local_registry = MetricsRegistry()
    with pytest.raises(ValueError) as excinfo:
        local_registry.inc("bad-name", None, 1)
    assert "bad-name" in str(excinfo.value)


def test_inc_accepts_valid_metric_names_including_colons() -> None:
    """Colons and leading underscores are both valid metric-name characters.

    ``ns:sub_metric_total`` is a common convention for pre-aggregated
    metrics; ``_underscore_start`` is used for internal-use metrics.
    Both must be accepted by the registry without raising.
    """
    local_registry = MetricsRegistry()
    local_registry.describe_counter("ns:sub_metric_total", "help")
    local_registry.describe_counter("_underscore_start", "help")
    local_registry.inc("ns:sub_metric_total")
    local_registry.inc("_underscore_start")
    assert local_registry.get_counter_value("ns:sub_metric_total", None) == 1.0
    assert local_registry.get_counter_value("_underscore_start", None) == 1.0


# ---------------------------------------------------------------------------
# Phase 2.8 — reset behavior
# ---------------------------------------------------------------------------


def test_reset_clears_observations_preserves_registrations() -> None:
    """``reset`` zeros counter values and histogram state but keeps registrations.

    After calling :meth:`MetricsRegistry.reset`, every previously
    incremented counter reads as ``0.0`` and every previously observed
    histogram has a zero sum and count. The HELP/TYPE declarations,
    however, remain in the rendered output because they represent the
    schema of the registry rather than observed values.
    """
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    local_registry.inc("c", {"k": "v"}, n=5)
    local_registry.describe_histogram("h", "latency")
    local_registry.observe("h", 0.05)

    local_registry.reset()

    assert local_registry.get_counter_value("c", {"k": "v"}) == 0.0
    assert local_registry.get_histogram_sum("h", None) == 0.0

    output = local_registry.render_prometheus()
    assert "# HELP c help" in output.splitlines()
    assert "# TYPE c counter" in output.splitlines()
    assert "# HELP h latency" in output.splitlines()
    assert "# TYPE h histogram" in output.splitlines()


# ---------------------------------------------------------------------------
# Phase 2.9 — render_prometheus output format
# ---------------------------------------------------------------------------


def test_render_prometheus_emits_help_and_type_lines_for_counters() -> None:
    """Counter rendering includes HELP, TYPE, and data lines in that order."""
    local_registry = MetricsRegistry()
    local_registry.describe_counter("requests_total", "Total HTTP requests")
    local_registry.inc("requests_total", {"endpoint": "x"})
    output = local_registry.render_prometheus()
    lines = output.splitlines()
    assert "# HELP requests_total Total HTTP requests" in lines
    assert "# TYPE requests_total counter" in lines
    assert 'requests_total{endpoint="x"} 1' in lines
    # Order constraint: HELP precedes TYPE precedes data.
    idx_help = lines.index("# HELP requests_total Total HTTP requests")
    idx_type = lines.index("# TYPE requests_total counter")
    idx_data = lines.index('requests_total{endpoint="x"} 1')
    assert idx_help < idx_type < idx_data


def test_render_prometheus_emits_help_and_type_lines_for_histograms() -> None:
    """Histogram rendering includes HELP/TYPE, bucket, sum, and count lines.

    Labels within a rendered line are sorted alphabetically: ``le`` (a
    histogram's built-in bucket-label key) sorts before an application
    label such as ``svc`` because ``l < s``.
    """
    local_registry = MetricsRegistry()
    local_registry.describe_histogram("latency", "Request latency", buckets=[0.1, 1.0])
    local_registry.observe("latency", 0.05, {"svc": "a"})
    output = local_registry.render_prometheus()
    lines = output.splitlines()

    assert "# HELP latency Request latency" in lines
    assert "# TYPE latency histogram" in lines
    # Label sort is alphabetical: "le" < "svc" -> le comes first.
    assert 'latency_bucket{le="0.1",svc="a"} 1' in lines
    assert 'latency_bucket{le="1",svc="a"} 1' in lines
    assert 'latency_bucket{le="+Inf",svc="a"} 1' in lines
    assert 'latency_sum{svc="a"} 0.05' in lines
    assert 'latency_count{svc="a"} 1' in lines


def test_render_prometheus_trailing_newline() -> None:
    """The rendered payload ends with exactly one newline character.

    Per the Prometheus exposition format, payloads should be
    newline-terminated. The production module joins with ``"\\n"`` and
    appends a trailing ``"\\n"``, so the output ends with a single
    newline and not two (which would produce an empty final line).
    """
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    output = local_registry.render_prometheus()
    assert output.endswith("\n")
    # Not two newlines.
    assert not output.endswith("\n\n")


def test_render_prometheus_sorts_metric_names() -> None:
    """Metrics are rendered in alphabetical order by metric name.

    Registering counters in reverse alphabetical order must nevertheless
    produce output where ``c_alpha`` precedes ``c_beta``.
    """
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c_beta", "help beta")
    local_registry.describe_counter("c_alpha", "help alpha")
    local_registry.inc("c_beta")
    local_registry.inc("c_alpha")
    output = local_registry.render_prometheus()
    assert output.index("c_alpha") < output.index("c_beta")


def test_render_prometheus_includes_registered_but_unincremented_counters() -> None:
    """A declared-but-never-incremented counter still emits HELP and TYPE.

    The production :func:`_render_counter` also emits a zero-valued
    data line ``<name> 0`` (no labels, no braces) so the counter has
    at least one sample at scrape time — the common Prometheus idiom
    to confirm presence without mandating a prior observation.
    """
    local_registry = MetricsRegistry()
    local_registry.describe_counter("untouched", "Never incremented")
    output = local_registry.render_prometheus()
    lines = output.splitlines()
    assert "# HELP untouched Never incremented" in lines
    assert "# TYPE untouched counter" in lines
    assert "untouched 0" in lines


def test_render_prometheus_empty_labels_no_brace_section() -> None:
    """A counter incremented with no labels renders without an empty ``{}``."""
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    local_registry.inc("c")
    output = local_registry.render_prometheus()
    lines = output.splitlines()
    assert "c 1" in lines
    # No empty brace section.
    assert "c{} 1" not in output


def test_render_prometheus_integer_value_rendered_without_trailing_zero() -> None:
    """Whole-number counter values are rendered as integers (no ``.0`` suffix).

    The production ``_format_value`` helper detects that ``3.0`` is a
    whole number and formats it as ``"3"`` to keep the exposition
    payload compact. This assertion locks in that behavior.
    """
    local_registry = MetricsRegistry()
    local_registry.describe_counter("c", "help")
    local_registry.inc("c", n=3)
    output = local_registry.render_prometheus()
    lines = output.splitlines()
    assert "c 3" in lines
    assert "c 3.0" not in lines


# ---------------------------------------------------------------------------
# Phase 2.10 - Thread safety
# ---------------------------------------------------------------------------


def test_inc_is_thread_safe() -> None:
    """Concurrent ``inc`` calls accumulate atomically under the registry lock.

    Spawns ``num_threads`` workers, each issuing
    ``increments_per_thread`` counter increments against a shared
    ``MetricsRegistry`` instance. After joining all workers, the final
    counter value must equal ``num_threads * increments_per_thread``;
    any lower value would indicate a lost update caused by a race
    inside the production ``inc`` critical section. Worker threads are
    joined with an explicit timeout so a deadlock in the production
    locking logic fails the test rather than hanging the suite, and the
    liveness assertion surfaces that failure mode immediately.
    """
    local_registry = MetricsRegistry()
    local_registry.describe_counter("counter", "thread-safety probe")

    num_threads = 10
    increments_per_thread = 100

    errors: List[BaseException] = []

    def _worker() -> None:
        try:
            for _ in range(increments_per_thread):
                local_registry.inc("counter", {"k": "v"})
        except BaseException as exc:  # noqa: E722
            # Capture any exception so the main thread can surface it;
            # bare ``except`` would violate flake8 but ``BaseException``
            # is the broadest acceptable catch at a thread boundary.
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    # Liveness check: no worker may still be running. A still-alive
    # thread after join(timeout=10) points at a deadlock in the
    # production locking logic and MUST fail this test.
    assert all(not t.is_alive() for t in threads), "one or more worker threads deadlocked"
    # No worker raised an exception.
    assert errors == []
    expected_total = float(num_threads * increments_per_thread)
    assert local_registry.get_counter_value("counter", {"k": "v"}) == expected_total


def test_observe_is_thread_safe() -> None:
    """Concurrent ``observe`` calls accumulate sum and count atomically.

    Spawns ``num_threads`` workers, each issuing
    ``observations_per_thread`` histogram observations with a fixed
    value against a shared ``MetricsRegistry`` instance. After joining
    all workers, the histogram ``sum`` must equal
    ``num_threads * observations_per_thread * value``; any lower value
    would indicate a lost update in the production ``observe`` critical
    section. Worker threads are joined with an explicit timeout so a
    deadlock fails the test rather than hanging the suite.
    """
    local_registry = MetricsRegistry()
    local_registry.describe_histogram("histogram", "thread-safety probe")

    num_threads = 10
    observations_per_thread = 100
    observation_value = 0.05

    errors: List[BaseException] = []

    def _worker() -> None:
        try:
            for _ in range(observations_per_thread):
                local_registry.observe("histogram", observation_value)
        except BaseException as exc:  # noqa: E722
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    # Liveness check: no worker may still be running.
    assert all(not t.is_alive() for t in threads), "one or more worker threads deadlocked"
    # No worker raised an exception.
    assert errors == []
    expected_sum = num_threads * observations_per_thread * observation_value
    # Floating-point accumulation tolerance.
    actual_sum = local_registry.get_histogram_sum("histogram", None)
    assert abs(actual_sum - expected_sum) < 1e-6


# ---------------------------------------------------------------------------
# Phase 2.11 - _freeze_labels helper (low-level canonicalization contract)
# ---------------------------------------------------------------------------


def test_freeze_labels_none_returns_empty_tuple() -> None:
    """``_freeze_labels(None)`` returns the empty tuple.

    Locks in the contract that a ``None`` label mapping is canonicalized
    to the same sentinel as an empty dict, so downstream code may use
    the frozen labels tuple as a dictionary key without preprocessing
    the ``None`` case.
    """
    assert metrics_module._freeze_labels(None) == ()


def test_freeze_labels_empty_dict_returns_empty_tuple() -> None:
    """``_freeze_labels({})`` returns the empty tuple.

    The empty dict and ``None`` share the same frozen representation,
    meaning ``inc("c")`` and ``inc("c", {})`` target the same counter
    bucket.
    """
    assert metrics_module._freeze_labels({}) == ()


def test_freeze_labels_sorts_keys() -> None:
    """``_freeze_labels`` produces a tuple of ``(key, value)`` pairs sorted by key.

    Sorting is essential for making two logically equivalent label sets
    (same keys and values but different insertion orders) hash to the
    same counter bucket, which underpins the Phase 2.3 key-order
    independence guarantee.
    """
    assert metrics_module._freeze_labels({"b": "2", "a": "1"}) == (("a", "1"), ("b", "2"))


def test_freeze_labels_coerces_values_to_strings() -> None:
    """``_freeze_labels`` coerces non-string label values to strings.

    Prometheus label values are defined as strings, so integers (and
    other scalar types) passed as label values must be converted via
    ``str()`` to avoid downstream type errors when the frozen tuple is
    used as a dictionary key or interpolated into the exposition text.
    """
    assert metrics_module._freeze_labels({"port": 8080}) == (("port", "8080"),)
