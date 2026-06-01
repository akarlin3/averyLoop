"""Structured run-event stream — one source, two renderers.

The improvement loop's progress used to be a wall of ``print`` calls.  This
module turns the high-level lifecycle (run/iteration/phase/scores/convergence,
and the multi-repo queue) into a small stream of :class:`Event` objects emitted
from a *single* source — the orchestrator — and consumed by interchangeable
renderers:

* :class:`PlainRenderer` — writes the same plain-text lines as before; used for
  non-TTY output (pipes, CI, log files) so those runs are byte-for-byte
  unsurprising.
* :class:`RichRenderer` — a live, in-place dashboard (``rich``) showing the repo
  queue, the current iteration/phase, the latest LLM + composite scores, and the
  convergence status.  It redirects stdout so the orchestrator's detailed
  per-finding prints still appear above the live region without corrupting it.

Renderer selection is automatic: Rich when stdout is a TTY and ``rich`` is
importable and ``AVERYLOOP_NO_TUI`` is unset; Plain otherwise.  The orchestrator
never knows which is active — it only calls :func:`emit`.  Detailed per-finding
prints remain raw ``print`` calls; only the dashboard-worthy lifecycle is
events, so the two never duplicate each other.

Tests use :class:`RecordingSink` to assert the exact ordered event stream of a
stubbed run, and assert the non-TTY path stays plain text.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import List, Optional

# The five pipeline phases, matching the orchestrator's historical [N/5] markers.
PHASE_TOTAL = 5


@dataclass
class Event:
    """A single structured progress event."""

    kind: str
    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------

class Sink:
    """Base renderer interface.  ``start``/``close`` bracket a run."""

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def handle(self, event: Event) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class RecordingSink(Sink):
    """Captures events in order — for tests."""

    def __init__(self) -> None:
        self.events: List[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> List[str]:
        return [e.kind for e in self.events]


class PlainRenderer(Sink):
    """Render events as plain-text lines (the historical stdout look)."""

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stdout

    def _w(self, line: str = "") -> None:
        print(line, file=self.stream)

    def handle(self, event: Event) -> None:
        d = event.data
        k = event.kind
        if k == "sweep_start":
            self._w()
            self._w("#" * 60)
            self._w(f"  Multi-repo sweep — {len(d.get('repos', []))} repo(s) "
                    f"({'DRY RUN' if d.get('dry_run') else 'LIVE'})")
            self._w("#" * 60)
        elif k == "repo_start":
            self._w()
            self._w(f">>> [{d.get('index')}/{d.get('total')}] repo: {d.get('name')}")
        elif k == "repo_end":
            self._w(f"<<< repo {d.get('name')}: {d.get('iterations')} iter(s), "
                    f"{d.get('fixes_merged')} merged — {d.get('reason', '')}")
        elif k == "run_start":
            self._w()
            self._w("=" * 60)
            self._w(f"  Improvement Loop [{d.get('project')}] — {d.get('mode')} "
                    f"(max {d.get('max_iterations')} iterations)")
            self._w("=" * 60)
        elif k == "iteration_start":
            self._w()
            self._w("─" * 60)
            self._w(f"  ITERATION {d.get('iteration')}/{d.get('max_iterations')}")
            self._w("─" * 60)
        elif k == "phase":
            self._w(f"[{d.get('n')}/{d.get('total', PHASE_TOTAL)}] {d.get('name')}")
        elif k == "findings":
            self._w(f"       Found {d.get('count')} valid finding(s)")
        elif k == "iteration_end":
            comp = d.get("composite")
            comp_s = f", composite={comp}" if comp is not None else ""
            self._w(f"  → iter {d.get('iteration')}: overall={d.get('overall')}"
                    f"{comp_s}, tests={'pass' if d.get('tests_passed') else 'FAIL'}")
        elif k == "convergence":
            if d.get("stop"):
                self._w(f"  Convergence detected: {d.get('reason')}")
        # run_end is intentionally silent: the orchestrator prints its own
        # full run summary, so emitting here would duplicate it.


class RichRenderer(Sink):
    """Live in-place dashboard backed by ``rich``."""

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stdout
        self._live = None
        self.project = ""
        self.max_iterations = 0
        self.current_repo = ""
        self.sweep: List[dict] = []          # repo queue (multi-repo only)
        self.rows: List[dict] = []           # iterations of the current repo
        self.convergence = ""

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        from rich.console import Console
        from rich.live import Live
        console = Console(file=self.stream)
        # redirect_stdout keeps the orchestrator's raw prints above the live
        # region instead of tearing through it.
        self._live = Live(self._render(), console=console, refresh_per_second=8,
                          redirect_stdout=True, redirect_stderr=True)
        self._live.start()

    def close(self) -> None:
        if self._live is not None:
            self._live.update(self._render())
            self._live.stop()
            self._live = None

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    # -- state updates -----------------------------------------------------
    def handle(self, event: Event) -> None:
        d = event.data
        k = event.kind
        if k == "sweep_start":
            self.sweep = [{"name": n, "status": "pending", "iters": 0,
                           "merged": 0, "reason": ""} for n in d.get("repos", [])]
        elif k == "repo_start":
            self.current_repo = d.get("name", "")
            self.rows = []
            self.convergence = ""
            for r in self.sweep:
                if r["name"] == self.current_repo:
                    r["status"] = "running"
        elif k == "repo_end":
            for r in self.sweep:
                if r["name"] == d.get("name"):
                    r.update(status="done", iters=d.get("iterations", 0),
                             merged=d.get("fixes_merged", 0),
                             reason=d.get("reason", ""))
        elif k == "run_start":
            self.project = d.get("project", "")
            self.max_iterations = d.get("max_iterations", 0)
        elif k == "iteration_start":
            self.rows.append({"iteration": d.get("iteration"), "phase": "",
                              "overall": "", "composite": "", "tests": "",
                              "status": "running"})
        elif k == "phase" and self.rows:
            self.rows[-1]["phase"] = f"{d.get('n')}/{d.get('total', PHASE_TOTAL)} " \
                                     f"{d.get('name', '')}"
        elif k == "iteration_end" and self.rows:
            row = self.rows[-1]
            row["overall"] = d.get("overall", "")
            row["composite"] = d.get("composite", "")
            row["tests"] = "pass" if d.get("tests_passed") else "FAIL"
            row["status"] = "exit" if d.get("exit") else "done"
        elif k == "convergence" and d.get("stop"):
            self.convergence = d.get("reason", "")
        self._refresh()

    # -- rendering ---------------------------------------------------------
    def _render(self):
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table

        blocks = []

        if self.sweep:
            q = Table(title="Repo queue", expand=False)
            q.add_column("repo")
            q.add_column("status")
            q.add_column("iters", justify="right")
            q.add_column("merged", justify="right")
            for r in self.sweep:
                marker = {"running": "▶", "done": "✓", "pending": "·"}.get(
                    r["status"], " ")
                q.add_row(f"{marker} {r['name']}", r["status"],
                          str(r["iters"]), str(r["merged"]))
            blocks.append(q)

        t = Table(expand=False)
        t.add_column("iter", justify="right")
        t.add_column("phase")
        t.add_column("LLM", justify="right")
        t.add_column("composite", justify="right")
        t.add_column("tests")
        t.add_column("status")
        for row in self.rows:
            t.add_row(str(row["iteration"]), str(row["phase"]),
                      str(row["overall"]), str(row["composite"]),
                      str(row["tests"]), str(row["status"]))

        title = f"AveryLoop — {self.current_repo or self.project or 'run'}"
        if self.max_iterations:
            title += f"  (max {self.max_iterations} iters)"
        blocks.append(t)
        if self.convergence:
            blocks.append(f"[yellow]Convergence:[/yellow] {self.convergence}")

        return Panel(Group(*blocks), title=title, border_style="cyan")


# ---------------------------------------------------------------------------
# Emitter + active-context plumbing
# ---------------------------------------------------------------------------

class Emitter:
    """Fans an event out to its sink and brackets a run via the context API."""

    def __init__(self, sink: Sink) -> None:
        self.sink = sink

    def emit(self, kind: str, **data) -> None:
        self.sink.handle(Event(kind, data))

    def __enter__(self) -> "Emitter":
        self.sink.start()
        _set_active(self)
        return self

    def __exit__(self, *exc) -> None:
        _set_active(None)
        self.sink.close()


_active: Optional[Emitter] = None
_DEFAULT_SINK = PlainRenderer()


def _set_active(em: Optional[Emitter]) -> None:
    global _active
    _active = em


def get_active() -> Optional[Emitter]:
    return _active


def _rich_available() -> bool:
    try:
        import rich  # noqa: F401
        return True
    except Exception:
        return False


def _is_tty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def make_emitter(stream=None, *, force_plain: Optional[bool] = None) -> Emitter:
    """Build an emitter, choosing Rich vs Plain automatically.

    Rich is used only when *stream* is a TTY, ``rich`` is importable, and
    ``AVERYLOOP_NO_TUI`` is unset; otherwise the plain renderer is used so piped
    and CI runs stay plain text.
    """
    stream = stream or sys.stdout
    if force_plain is None:
        force_plain = os.environ.get("AVERYLOOP_NO_TUI") == "1"
    use_rich = (not force_plain) and _is_tty(stream) and _rich_available()
    sink: Sink = RichRenderer(stream) if use_rich else PlainRenderer(stream)
    return Emitter(sink)


@contextmanager
def ensure_emitter(stream=None, *, force_plain: Optional[bool] = None):
    """Yield the active emitter, creating + activating one if none exists.

    The outermost caller owns the emitter (so a multi-repo sweep and the
    per-repo loop share one dashboard rather than nesting two live displays).
    """
    existing = get_active()
    if existing is not None:
        yield existing
        return
    with make_emitter(stream, force_plain=force_plain) as em:
        yield em


def emit(kind: str, **data) -> None:
    """Emit an event to the active emitter, or plainly to stdout if none."""
    em = get_active()
    if em is None:
        _DEFAULT_SINK.handle(Event(kind, data))
    else:
        em.emit(kind, **data)
