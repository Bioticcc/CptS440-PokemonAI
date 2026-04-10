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
) -> bool:
    ps_client = getattr(player, "ps_client", None)
    search_ladder_game = getattr(ps_client, "search_ladder_game", None)
    if ps_client is None or not callable(search_ladder_game):
        return False

    battle_format = str(getattr(player, "format", "") or getattr(player, "_configured_battle_format", "gen1randombattle"))
    team = getattr(player, "next_team", None)
    if callable(team):
        try:
            team = team()
        except Exception:
            team = None

    try:
        future = asyncio.run_coroutine_threadsafe(
            search_ladder_game(battle_format, team),
            POKE_LOOP,
        )
        future.result(timeout=5.0)
        if verbose:
            print(f"[{phase_label}] requeued ladder search format={battle_format}")
        return True
    except Exception as exc:
        if verbose:
            print(
                f"[{phase_label}] requeue_failed error={type(exc).__name__}: {exc}"
            )
        return False


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
