"""Sensor per camera 'Animal': state = dog, cat or none."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import CameraState, PetRecognitionManager


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    mgr: PetRecognitionManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(PetLabelSensor(mgr, c) for c in mgr.cameras)


class PetBase:
    _attr_has_entity_name = True
    _attr_should_poll = False
    _suffix = ""

    def __init__(self, mgr: PetRecognitionManager, camera: str) -> None:
        self._mgr = mgr
        self._camera = camera
        self._attr_unique_id = f"{mgr.entry_id}_{camera}_{self._suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mgr.entry_id)}, name="NKD Pet Recognition"
        )

    @property
    def _state(self) -> CameraState:
        return self._mgr.cameras[self._camera]

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._mgr.signal, self._refresh)
        )

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()


class PetLabelSensor(PetBase, SensorEntity):
    _suffix = "label"

    def __init__(self, mgr: PetRecognitionManager, camera: str) -> None:
        super().__init__(mgr, camera)
        self._attr_name = f"{camera.split('.', 1)[-1].replace('_', ' ').title()} animal"

    @property
    def native_value(self) -> str | None:
        r = self._state.result
        return r.label if r else None

    @property
    def extra_state_attributes(self) -> dict:
        s = self._state
        r = s.result
        return {
            "camera": s.entity_id,
            "confidence": r.confidence if r else None,
            "species": r.label if r else None,
            "animals": r.animals if r else [],
            "breed": r.breed if r else None,
            "breeds": r.breeds if r else [],
            "models_agree": r.models_agree if r else None,
            "quota": r.usage if r else {},
            "last_success": s.last_success.isoformat() if s.last_success else None,
            "last_error": s.last_error,
            "stale": s.stale,
            "consecutive_failures": s.failures,
        }
