"""TUI dashboard for oparl-bridge-sync using Rich."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta

from rich.columns import Columns
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskID,
    TextColumn,
)
from rich.table import Table
from rich.text import Text


class _EtaColumn(ProgressColumn):
    """Shows absolute ETA clock instead of remaining duration."""
    def render(self, task: Task) -> Text:
        if task.finished or task.time_remaining is None:
            return Text("")
        eta = datetime.now() + timedelta(seconds=task.time_remaining)
        return Text(f"ETA {eta.strftime('%H:%M')}", style="cyan")

_STATUS_ICON = {
    "pending": ("[dim]○[/]", "dim"),
    "running": ("[yellow]▶[/]", ""),
    "done":    ("[green]✓[/]", ""),
    "error":   ("[red]✗[/]", "red"),
    "skipped": ("[dim]–[/]", "dim"),
}

_LOG_COLOR = {"INFO": "green", "WARNING": "yellow", "ERROR": "red", "DEBUG": "dim"}


class _LogHandler(logging.Handler):
    def __init__(self, buf: deque[Text], req_items: deque[list[str]]) -> None:
        super().__init__()
        self.buf = buf
        # Each entry: [request_markup, result_markup]  — result starts empty, filled in-place
        self.req_items = req_items

    def emit(self, record: logging.LogRecord) -> None:
        color = _LOG_COLOR.get(record.levelname, "white")
        msg = record.getMessage()
        # Suppress per-item progress lines — the bar covers those
        if msg.startswith("  ") and "/" in msg and "%" in msg:
            return
        # ▶  →  new pending request row
        if msg.startswith("▶ "):
            self.req_items.append(["[cyan]▶[/] " + msg[2:], ""])
            return
        # ✓ / ✗  →  fill result into the last pending row (or open a new one)
        if msg.startswith(("✓ ", "✗ ")):
            clr = "green" if msg[0] == "✓" else "red"
            res = f"[{clr}]{msg[0]}[/] {msg[2:]}"
            if self.req_items and self.req_items[-1][1] == "":
                self.req_items[-1][1] = res
            else:
                self.req_items.append(["", res])
            return
        self.buf.append(Text.from_markup(
            f"[{color}]{record.levelname:<7}[/] {msg}"
        ))


class _Renderable:
    """Wrapper so Live re-calls _render() on every auto-refresh tick."""
    def __init__(self, dash: Dashboard) -> None:
        self.dash = dash

    def __rich_console__(self, console, options):  # noqa: ANN001
        yield self.dash._render()


class Dashboard:
    """Rich Live TUI for sync progress and log output.

    Usage::

        with Dashboard("sync") as dash:
            dash.set_phase("action=1", "running")
            ...
            dash.set_phase("action=1", "done", "83 Gremien")
    """

    def __init__(self, command: str, body_name: str = "") -> None:
        self.command = command
        self.body_name = body_name
        self._phases: list[tuple[str, str, str]] = []  # (label, status, detail)
        self._log_buf: deque[Text] = deque(maxlen=8)
        self._req_items: deque[list[str]] = deque(maxlen=5)
        self._progress = Progress(
            SpinnerColumn(),
            "[progress.description]{task.description}",
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            "[progress.percentage]{task.percentage:>3.0f}%",
            _EtaColumn(),
            refresh_per_second=4,
        )
        self._org_progress = Progress(
            TextColumn("Gremien", style="dim"),
            BarColumn(bar_width=20),
            MofNCompleteColumn(),
            TextColumn("{task.description}", style="dim"),
            refresh_per_second=4,
        )
        self._task_id: TaskID | None = None
        self._org_task_id: TaskID | None = None
        self._live: Live | None = None
        self._log_handler = _LogHandler(self._log_buf, self._req_items)
        self._log_handler.setLevel(logging.DEBUG)
        self._shutdown: asyncio.Event = asyncio.Event()
        self._started_at = datetime.now()
        self._aborted = False

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def shutdown(self) -> asyncio.Event:
        return self._shutdown

    def add_phase(self, label: str, status: str = "pending", detail: str = "") -> None:
        self._phases.append((label, status, detail))

    def set_phase(self, label: str, status: str, detail: str = "") -> None:
        for i, (lbl, _s, d) in enumerate(self._phases):
            if lbl == label:
                self._phases[i] = (label, status, detail if detail else d)
                return
        self._phases.append((label, status, detail))

    def start_task(self, description: str, total: int) -> Callable[[int, int], None]:
        """Start a progress task and return an on_progress callback."""
        if self._task_id is not None:
            self._progress.remove_task(self._task_id)
        task_id = self._progress.add_task(description, total=total)
        self._task_id = task_id

        def _cb(current: int, _total: int) -> None:
            self._progress.update(task_id, completed=current, total=_total)

        return _cb

    def finish_task(self) -> None:
        if self._aborted:
            return
        if self._task_id is not None:
            task = next((t for t in self._progress.tasks if t.id == self._task_id), None)
            if task is not None:
                self._progress.update(self._task_id, completed=task.total)

    def start_org_task(self) -> Callable[[str, int, int], None]:
        """Create org-level progress bar and return an on_org(name, current, total) callback."""
        org_task_id = self._org_progress.add_task("", total=1)
        self._org_task_id = org_task_id

        def _cb(org_name: str, current: int, total: int) -> None:
            self._org_progress.update(
                org_task_id,
                description=org_name,
                completed=current,
                total=total,
            )

        return _cb

    # ── rendering ─────────────────────────────────────────────────────────────

    def _render(self) -> Group:
        elapsed = int((datetime.now() - self._started_at).total_seconds())
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
        title = (
            f"[bold]{self.body_name}[/bold]  ·  [cyan]{self.command}[/cyan]"
            if self.body_name else f"[cyan]{self.command}[/cyan]"
        )
        header = Panel(
            Columns([
                Text.from_markup(title),
                Text(
                    f"{self._started_at.strftime('%d.%m.%Y %H:%M')}  +{h:02d}:{m:02d}:{s:02d}",
                    justify="right",
                ),
            ], expand=True),
            style="bold blue",
            padding=(0, 1),
        )

        table = Table.grid(padding=(0, 2))
        table.add_column(width=2)
        table.add_column(min_width=18)
        table.add_column()
        for label, status, detail in self._phases:
            icon, style = _STATUS_ICON.get(status, ("?", ""))
            table.add_row(
                Text.from_markup(icon),
                Text.from_markup(f"[{style}]{label}[/]" if style else label),
                Text(detail, style="dim"),
            )

        req_grid = Table.grid(padding=(0, 3))
        req_grid.add_column(min_width=32, no_wrap=True)
        req_grid.add_column(no_wrap=True)
        if self._req_items:
            for req_str, res_str in self._req_items:
                req_grid.add_row(
                    Text.from_markup(req_str) if req_str else Text(""),
                    Text.from_markup(res_str) if res_str else Text("…", style="dim"),
                )
        else:
            req_grid.add_row(Text("–", style="dim"), Text(""))

        log_lines = list(self._log_buf) or [Text("–", style="dim")]
        log_panel = Panel(
            Group(
                Text("Requests", style="dim underline"),
                req_grid,
                Text(""),
                Text("Log", style="dim underline"),
                *log_lines,
            ),
            border_style="dim",
            padding=(0, 1),
        )

        if self._aborted:
            footer = Text("  Abgebrochen — bisheriger Stand gespeichert.", style="yellow")
        else:
            footer = Text("  [STRG+C] Abbrechen — aktueller Stand wird gespeichert", style="dim")

        parts: list = [header, table]
        if self._task_id is not None:
            parts.append(self._progress)
        if self._org_task_id is not None:
            parts.append(self._org_progress)
        parts.extend([log_panel, footer])
        return Group(*parts)

    # ── context manager ────────────────────────────────────────────────────────

    def __enter__(self) -> Dashboard:
        _logger = logging.getLogger("oparl_bridge")
        _logger.addHandler(self._log_handler)
        # Allow DEBUG through so ▶ request markers reach the handler
        if _logger.level == logging.NOTSET or _logger.level > logging.DEBUG:
            _logger.setLevel(logging.DEBUG)
        loop = asyncio.get_event_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, self._on_sigint)
        except (NotImplementedError, RuntimeError):
            pass  # Windows or no event loop
        self._live = Live(
            _Renderable(self),
            refresh_per_second=4,
            screen=False,
            redirect_stderr=False,
        )
        self._live.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        if self._live:
            self._live.__exit__(*args)
        _logger = logging.getLogger("oparl_bridge")
        _logger.removeHandler(self._log_handler)
        _logger.setLevel(logging.NOTSET)
        loop = asyncio.get_event_loop()
        try:
            loop.remove_signal_handler(signal.SIGINT)
        except (NotImplementedError, RuntimeError):
            pass

    def _on_sigint(self) -> None:
        self._shutdown.set()
        self._aborted = True
        self._log_buf.append(Text.from_markup(
            "[yellow]WARNING [/] STRG+C — stoppe nach aktuellem Schritt …"
        ))


def use_tui() -> bool:
    """Return True when a TUI makes sense (interactive TTY, rich available)."""
    return sys.stdout.isatty() and sys.stderr.isatty()
