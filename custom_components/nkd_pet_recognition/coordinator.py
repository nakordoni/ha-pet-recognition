"""Polling manager: per-camera state, back-off, stale handling, events.

Safety rules implemented here:
* errors never change the last good label/confidence, they only set stale
* the binary sensors (dog, cat) derive from last GOOD data only, never from an error
* after ``max_failures`` consecutive failed polls the binary sensors are unavailable
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.components.camera import async_get_image
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api_client import (
    LABEL_DOG,
    PetApiAuthError,
    PetApiError,
    PetRecognitionClient,
    PetResult,
)
from .const import (
    DOMAIN,
    EVENT_DETECTED,
    IMAGE_TIMEOUT,
    LABEL_NONE,
    MAX_BACKOFF,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class CameraState:
    """State of one camera."""

    entity_id: str
    result: PetResult | None = None  # last GOOD result
    last_success: datetime | None = None
    last_error: str | None = None
    stale: bool = False
    failures: int = 0  # consecutive failed polls
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    unsub: CALLBACK_TYPE | None = None


class PetRecognitionManager:
    """Owns the polling loop for all cameras of one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client: PetRecognitionClient,
        cameras: list[str],
        interval: int,
        threshold: float,
        max_failures: int,
        on_auth_failed: Callable[[], None] | None = None,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.client = client
        self.interval = interval
        self.threshold = threshold
        self.max_failures = max_failures
        self._on_auth_failed = on_auth_failed
        self.cameras: dict[str, CameraState] = {c: CameraState(c) for c in cameras}
        self._stopped = False

    @property
    def signal(self) -> str:
        return f"{DOMAIN}_update_{self.entry_id}"

    # ---- lifecycle -------------------------------------------------------
    @callback
    def async_start(self) -> None:
        for state in self.cameras.values():
            self._schedule(state, 1)

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        for state in self.cameras.values():
            if state.unsub:
                state.unsub()
                state.unsub = None

    @callback
    def _schedule(self, state: CameraState, delay: float) -> None:
        if self._stopped:
            return
        if state.unsub:
            state.unsub()

        async def _run(_now: datetime) -> None:
            state.unsub = None
            await self.async_poll(state.entity_id, scheduled=True)

        state.unsub = async_call_later(self.hass, delay, _run)

    # ---- derived values --------------------------------------------------
    def is_detected(self, state: CameraState, species: str = LABEL_DOG) -> bool | None:
        """None = unavailable. Derived from last good result only.

        ON when ``species`` (dog or cat) is in the photo with score >= threshold.
        """
        if state.result is None or state.failures >= self.max_failures:
            return None
        return state.result.score(species) >= self.threshold

    # ---- polling ---------------------------------------------------------
    async def async_poll(self, entity_id: str, scheduled: bool = False) -> PetResult:
        """Analyze one camera now. Raises HomeAssistantError on failure (manual calls)."""
        state = self.cameras[entity_id]
        async with state.lock:
            delay = float(self.interval)
            error: Exception | None = None
            result: PetResult | None = None
            try:
                try:
                    image = await async_get_image(
                        self.hass, entity_id, timeout=IMAGE_TIMEOUT
                    )
                except (HomeAssistantError, asyncio.TimeoutError) as err:
                    raise _CameraError(type(err).__name__) from None
                result = await self.client.analyze(image.content, image.content_type)
            except Exception as err:  # noqa: BLE001 - everything is a failed poll
                error = err
                delay = self._on_failure(state, err)
            else:
                self._on_success(state, result)

            async_dispatcher_send(self.hass, self.signal)
            if scheduled:
                self._schedule(state, delay)
        if error is not None:
            raise HomeAssistantError(f"analysis failed: {state.last_error}")
        assert result is not None
        return result

    def _on_success(self, state: CameraState, result: PetResult) -> None:
        state.result = result
        state.last_success = dt_util.utcnow()
        state.last_error = None
        state.stale = False
        state.failures = 0
        if result.label != LABEL_NONE and result.confidence >= self.threshold:
            self.hass.bus.async_fire(
                EVENT_DETECTED,
                {
                    "camera": state.entity_id,
                    "label": result.label,
                    "species": result.label,
                    "confidence": result.confidence,
                    "animals": result.animals,
                },
            )

    def _on_failure(self, state: CameraState, err: Exception) -> float:
        """Record failure; keep last good data, mark stale. Returns next delay."""
        state.failures += 1
        state.stale = True  # never touches result -> no flip to ON from an error
        if isinstance(err, PetApiError):
            state.last_error = str(err)
        else:
            state.last_error = type(err).__name__ + (
                f": {err}" if isinstance(err, _CameraError) else ""
            )
        _LOGGER.warning(
            "Poll failed for %s (%s), consecutive failures: %s",
            state.entity_id,
            state.last_error,
            state.failures,
        )
        backoff = min(self.interval * (2 ** state.failures), MAX_BACKOFF)
        if isinstance(err, PetApiAuthError):
            backoff = MAX_BACKOFF
            # HA shows "re-enter key" (same effect as ConfigEntryAuthFailed
            # raised from a coordinator; our loop is not a coordinator).
            if self._on_auth_failed:
                self._on_auth_failed()
        retry_after = getattr(err, "retry_after", None) or 0
        return max(float(self.interval), backoff, float(retry_after))


class _CameraError(Exception):
    """Camera snapshot failed."""
