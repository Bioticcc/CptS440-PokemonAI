"""Connection and recovery helpers for Showdown ladder runtime."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from poke_env.concurrency import POKE_LOOP


class AsyncConnectionRunner:

    # Keeps showdown ladder connection running in background while other loop logic runs.

    def __init__(self, player: Any, n_games: int | None = 1) -> None:
        self._player = player
        self._n_games = None if n_games is None else int(n_games)
        if self._n_games is not None and self._n_games <= 0:
            raise ValueError("n_games must be positive or None")
        self._done_event = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            asyncio.run(self._run_coro())
        except BaseException as exc:  # pragma: no cover - defensive bridge from thread
            self._error = exc
        finally:
            self._done_event.set()

    async def _run_coro(self) -> None:
        games = self._n_games or 1
        await self._player.ladder(games)

    def start(self) -> "AsyncConnectionRunner":
        self._thread.start()
        return self

    @property
    def done(self) -> bool:
        return self._done_event.is_set()

    @property
    def error(self) -> BaseException | None:
        return self._error

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError("connection runner failed") from self._error


def _safe_reset_battles(player: Any) -> None:
    reset_fn = getattr(player, "reset_battles", None)
    if not callable(reset_fn):
        return
    try:
        reset_fn()
    except Exception:
        return


def _is_recoverable_connection_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    message = str(exc).lower()
    markers = (
        "keepalive ping timeout",
        "connectionclosed",
        "websocket",
        "no close frame received",
        "connection is closed",
        "sent 1011",
    )
    return any(marker in message for marker in markers)


def _safe_clear_all_battles(player: Any) -> None:
    battle_map = getattr(player, "_battles", None)
    if isinstance(battle_map, dict):
        battle_map.clear()

    pending_orders = getattr(player, "_pending_orders", None)
    if isinstance(pending_orders, dict):
        pending_orders.clear()


def _safe_restart_showdown_listener(
    player: Any,
    *,
    phase_label: str,
    verbose: bool = False,
) -> bool:
    ps_client = getattr(player, "ps_client", None)
    if ps_client is None:
        return False

    listening_future = getattr(ps_client, "_listening_coroutine", None)
    if listening_future is not None:
        try:
            if not listening_future.done():
                listening_future.cancel()
        except Exception:
            pass

    try:
        new_future = asyncio.run_coroutine_threadsafe(ps_client.listen(), POKE_LOOP)
    except Exception as exc:
        if verbose:
            print(
                f"[{phase_label}] reconnect_failed error={type(exc).__name__}: {exc}"
            )
        return False

    try:
        setattr(ps_client, "_listening_coroutine", new_future)
    except Exception:
        pass

    if verbose:
        print(f"[{phase_label}] reconnect_started")
    time.sleep(1.0)
    return True


def _safe_requeue_ladder_search(
    player: Any,
    *,
    phase_label: str,
    verbose: bool = False,
    force: bool = False,
) -> bool:
    ps_client = getattr(player, "ps_client", None)
    search_ladder_game = getattr(ps_client, "search_ladder_game", None)
    if ps_client is None or not callable(search_ladder_game):
        return False

    listening_future = getattr(ps_client, "_listening_coroutine", None)
    listener_done = True
    if listening_future is not None:
        try:
            listener_done = bool(listening_future.done())
        except Exception:
            listener_done = True

    websocket = getattr(ps_client, "websocket", None)
    websocket_close_code = getattr(websocket, "close_code", None) if websocket is not None else None
    websocket_closed = websocket is None or websocket_close_code is not None
    connection_healthy = not listener_done and not websocket_closed
    # Safety behavior:
    # - Healthy + non-forced call: avoid extra /search probes to prevent duplicate queues.
    # - Healthy + forced call (after prolonged idle): run a controlled cancel/search probe.
    if connection_healthy and not force:
        if verbose:
            print(f"[{phase_label}] queue appears healthy; skipping requeue probe")
        return True

    if connection_healthy and force and verbose:
        print(f"[{phase_label}] queue appears healthy; running forced cancel/search probe")

    if not connection_healthy:
        if force and verbose:
            print(f"[{phase_label}] forcing ladder recovery after prolonged idle")
        _safe_restart_showdown_listener(player, phase_label=phase_label, verbose=verbose)

    battle_format = str(getattr(player, "format", "") or getattr(player, "_configured_battle_format", "gen1randombattle"))
    team = getattr(player, "next_team", None)
    if callable(team):
        try:
            team = team()
        except Exception:
            team = None
    send_message = getattr(ps_client, "send_message", None)

    def _attempt_requeue(*, cancel_first: bool) -> None:
        if cancel_first and callable(send_message):
            try:
                cancel_future = asyncio.run_coroutine_threadsafe(
                    send_message("/cancelsearch"),
                    POKE_LOOP,
                )
                cancel_future.result(timeout=2.0)
            except Exception:
                pass
        future = asyncio.run_coroutine_threadsafe(
            search_ladder_game(battle_format, team),
            POKE_LOOP,
        )
        future.result(timeout=5.0)

    try:
        _attempt_requeue(cancel_first=True)
        if verbose:
            print(f"[{phase_label}] requeued ladder search format={battle_format}")
        return True
    except Exception as exc:
        if _is_recoverable_connection_error(exc):
            _safe_restart_showdown_listener(player, phase_label=phase_label, verbose=verbose)
            try:
                _attempt_requeue(cancel_first=True)
                if verbose:
                    print(f"[{phase_label}] requeued ladder search after reconnect format={battle_format}")
                return True
            except Exception as retry_exc:
                exc = retry_exc
        if verbose:
            print(
                f"[{phase_label}] requeue_failed error={type(exc).__name__}: {exc}"
            )
        return False


def _maybe_requeue_on_idle(
    player: Any,
    *,
    runner: AsyncConnectionRunner | None,
    runner_started_at: float | None,
    idle_requeue_attempts: int,
    phase_label: str,
    verbose: bool = False,
    idle_threshold_seconds: float = 45.0,
    force_after_attempts: int = 4,
) -> tuple[float | None, int]:
    if runner is None or runner.done:
        return runner_started_at, idle_requeue_attempts

    now = time.time()
    started_at = float(runner_started_at) if runner_started_at is not None else now
    idle_seconds = now - started_at
    if idle_seconds < max(1.0, float(idle_threshold_seconds)):
        return runner_started_at, idle_requeue_attempts

    attempts = int(idle_requeue_attempts) + 1
    force_recovery = attempts >= max(1, int(force_after_attempts))
    if verbose:
        print(
            f"[{phase_label}] idle_without_battle for {idle_seconds:.1f}s; "
            "attempting ladder requeue"
        )
        if force_recovery:
            print(
                f"[{phase_label}] prolonged idle detected "
                f"(attempt={attempts}); forcing recovery"
            )

    _safe_requeue_ladder_search(
        player,
        phase_label=phase_label,
        verbose=verbose,
        force=force_recovery,
    )
    return time.time(), attempts


def _safe_ensure_battle_timer_on(
    player: Any,
    battle_tag: str,
    *,
    phase_label: str,
    verbose: bool = False,
    min_interval_seconds: float = 10.0,
) -> bool:
    normalized_tag = str(battle_tag or "")
    if not normalized_tag:
        return False

    timer_sent_at = getattr(player, "_timer_on_sent_at", None)
    if not isinstance(timer_sent_at, dict):
        timer_sent_at = {}
        try:
            setattr(player, "_timer_on_sent_at", timer_sent_at)
        except Exception:
            pass

    now = time.time()
    last_sent_at = float(timer_sent_at.get(normalized_tag, 0.0))
    if now - last_sent_at < max(0.0, float(min_interval_seconds)):
        return False

    ps_client = getattr(player, "ps_client", None)
    send_message = getattr(ps_client, "send_message", None)
    websocket = getattr(ps_client, "websocket", None)
    if ps_client is None or websocket is None or not callable(send_message):
        return False

    try:
        future = asyncio.run_coroutine_threadsafe(
            send_message("/timer on", normalized_tag),
            POKE_LOOP,
        )
        future.result(timeout=2.0)
        timer_sent_at[normalized_tag] = now
        return True
    except Exception as exc:
        if verbose:
            print(
                f"[{phase_label}] timer_on_failed battle={normalized_tag} "
                f"error={type(exc).__name__}: {exc}"
            )
        return False


def _resolve_runner_state(
    runner: AsyncConnectionRunner | None,
    *,
    player: Any,
    phase_label: str,
    verbose: bool = False,
) -> AsyncConnectionRunner | None:
    if runner is None or not runner.done:
        return runner

    runner_error = runner.error
    if runner_error is None:
        return None

    if _is_recoverable_connection_error(runner_error):
        if verbose:
            print(
                f"[{phase_label}] connection dropped "
                f"({type(runner_error).__name__}: {runner_error}). Reconnecting."
            )
        _safe_clear_all_battles(player)
        _safe_restart_showdown_listener(player, phase_label=phase_label, verbose=verbose)
        return None

    runner.raise_if_failed()
    return None


def _safe_cleanup_finished_battle(
    player: Any,
    battle_tag: str,
    *,
    phase_label: str | None = None,
    verbose: bool = False,
) -> None:
    normalized_tag = str(battle_tag)

    # Best effort: ask showdown to leave the room for this finished battle.
    ps_client = getattr(player, "ps_client", None)
    send_message = getattr(ps_client, "send_message", None)
    websocket = getattr(ps_client, "websocket", None)
    if websocket is not None and callable(send_message):
        try:
            future = asyncio.run_coroutine_threadsafe(
                send_message(f"/leave {normalized_tag}"),
                POKE_LOOP,
            )
            future.result(timeout=1.0)
        except Exception:
            pass

    # Important: do not remove entries from player._battles here.
    # poke-env may still receive trailing messages for a finished battle tag, and
    # removing the object early can stall its internal battle-message handler.

    pending_orders = getattr(player, "_pending_orders", None)
    if isinstance(pending_orders, dict):
        pending_orders.pop(normalized_tag, None)

    timer_sent_at = getattr(player, "_timer_on_sent_at", None)
    if isinstance(timer_sent_at, dict):
        timer_sent_at.pop(normalized_tag, None)

    if verbose and phase_label:
        print(f"[{phase_label}] cleaned_up battle={normalized_tag}")


def _launch_single_game(player: Any) -> AsyncConnectionRunner:
    return AsyncConnectionRunner(player, 1).start()


def _safe_wait_until_logged_in(player: Any, *, timeout_seconds: float = 30.0) -> bool:
    ps_client = getattr(player, "ps_client", None)
    logged_in_event = getattr(ps_client, "logged_in", None)
    loop = getattr(ps_client, "loop", None)
    if ps_client is None or logged_in_event is None or loop is None:
        return False
    if logged_in_event.is_set():
        return True
    try:
        future = asyncio.run_coroutine_threadsafe(logged_in_event.wait(), loop)
        future.result(timeout=max(1.0, timeout_seconds))
        return True
    except Exception:
        return False


def _safe_send_challenge(
    player: Any,
    opponent_name: str,
    *,
    phase_label: str = "manual",
) -> bool:
    opponent = str(opponent_name or "").strip()
    if not opponent:
        return False
    if not _safe_wait_until_logged_in(player):
        print(f"[{phase_label}] failed to confirm logged-in session before challenge.")
        return False

    ps_client = getattr(player, "ps_client", None)
    loop = getattr(ps_client, "loop", None)
    if ps_client is None or loop is None:
        return False

    packed_team = player.get_next_team() if hasattr(player, "get_next_team") else None
    battle_format = str(
        getattr(player, "format", "")
        or getattr(player, "_configured_battle_format", "gen1randombattle")
    )
    try:
        future = asyncio.run_coroutine_threadsafe(
            ps_client.challenge(opponent, battle_format, packed_team),
            loop,
        )
        future.result(timeout=10.0)
        return True
    except Exception as exc:
        print(f"[{phase_label}] challenge_failed error={type(exc).__name__}: {exc}")
        return False


def _wait_for_active_battle(
    player: Any,
    *,
    phase_label: str,
    waiting_message: str,
    runner: AsyncConnectionRunner | None = None,
    allow_requeue: bool = False,
    timeout_seconds: float = 180.0,
    verbose: bool = True,
) -> tuple[Any | None, AsyncConnectionRunner | None]:
    wait_started_at = time.time()
    runner_started_at = time.time()
    idle_requeue_attempts = 0
    last_wait_notice_at = 0.0

    while True:
        runner = _resolve_runner_state(
            runner,
            player=player,
            phase_label=phase_label,
            verbose=verbose,
        )

        battles = dict(getattr(player, "battles", {}) or {})
        for battle in battles.values():
            if not getattr(battle, "finished", False):
                return battle, runner

        now = time.time()
        if verbose and (now - last_wait_notice_at >= 5.0):
            elapsed = now - wait_started_at
            print(f"[{phase_label}] {waiting_message} ({elapsed:.1f}s)")
            last_wait_notice_at = now

        if now - wait_started_at >= max(1.0, timeout_seconds):
            return None, runner

        if allow_requeue and runner is not None and not runner.done:
            runner_started_at, idle_requeue_attempts = _maybe_requeue_on_idle(
                player,
                runner=runner,
                runner_started_at=runner_started_at,
                idle_requeue_attempts=idle_requeue_attempts,
                phase_label=phase_label,
                verbose=verbose,
            )

        time.sleep(0.2)
