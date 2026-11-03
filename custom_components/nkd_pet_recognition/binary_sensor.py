"""Binary sensors 'dog detected' and 'cat detected'.

ON only when the last GOOD result has that species in `animals` with score >=
threshold. Errors never set it ON. Unavailable after N consecutive failed polls.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api_client import LABEL_CAT, LABEL_DOG
from .const import DOMAIN
from .coordinator import PetRecognitionManager
from .sensor import PetBase


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    mgr: PetRecognitionManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        cls(mgr, c) for c in mgr.cameras for cls in (PetDetectedBinarySensor, PetCatDetectedBinarySensor)
    )


class PetDetectedBinarySensor(PetBase, BinarySensorEntity):
    """Dog detected. unique_id suffix `detected` is kept for existing installs."""

    _suffix = "detected"
    _species = LABEL_DOG
    _name_part = "dog detected"

    def __init__(self, mgr: PetRecognitionManager, camera: str) -> None:
        super().__init__(mgr, camera)
        self._attr_name = f"{camera.split('.', 1)[-1].replace('_', ' ').title()} {self._name_part}"

    @property
    def available(self) -> bool:
        return self._mgr.is_detected(self._state, self._species) is not None

    @property
    def is_on(self) -> bool | None:
        return self._mgr.is_detected(self._state, self._species)

    @property
    def extra_state_attributes(self) -> dict:
        s = self._state
        r = s.result
        return {
            "species": self._species,
            "score": r.score(self._species) if r else None,
            "label": r.label if r else None,
            "confidence": r.confidence if r else None,
            "threshold": self._mgr.threshold,
            "stale": s.stale,
            "last_success": s.last_success.isoformat() if s.last_success else None,
            "last_error": s.last_error,
            "consecutive_failures": s.failures,
        }


class PetCatDetectedBinarySensor(PetDetectedBinarySensor):
    """Cat detected (new in 0.2.0)."""

    _suffix = "cat_detected"
    _species = LABEL_CAT
    _name_part = "cat detected"
