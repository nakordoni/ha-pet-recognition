from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.data_entry_flow import FlowResultType

from custom_components.nkd_pet_recognition.api_client import (
    PetApiTransientError,
    PetResult,
    parse_retry_after,
)
from custom_components.nkd_pet_recognition.const import DOMAIN
from custom_components.nkd_pet_recognition.coordinator import PetRecognitionManager


async def test_config_flow(hass, aioclient_mock):
    from custom_components.nkd_pet_recognition import api_client as _a
    aioclient_mock.post(_a.API_URL, status=400, json={"ok": False, "error": {"code": "image_required"}})
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] == FlowResultType.FORM
    with patch("custom_components.nkd_pet_recognition.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"],
            {"api_key": "k", "cameras": ["camera.a"], "interval": 30,
             "threshold": 0.7, "max_failures": 3},
        )
    assert r["type"] == FlowResultType.CREATE_ENTRY
    assert r["data"]["cameras"] == ["camera.a"]


def test_retry_after():
    assert parse_retry_after("7") == 7
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("999999") == 3600


async def test_errors_never_turn_on_and_go_unavailable(hass):
    client = MagicMock()
    client.analyze = AsyncMock(side_effect=[
        PetResult("none", 0.0, []),
        PetApiTransientError("HTTP 503", 5),
        PetApiTransientError("timeout"),
        PetApiTransientError("timeout"),
    ])
    m = PetRecognitionManager(hass, "e", client, ["camera.a"], 30, 0.7, 3)
    st = m.cameras["camera.a"]
    img = MagicMock(content=b"x", content_type="image/jpeg")
    with patch("custom_components.nkd_pet_recognition.coordinator.async_get_image",
               AsyncMock(return_value=img)):
        await m.async_poll("camera.a")
        assert m.is_detected(st) is False and not st.stale
        for i in range(3):
            try:
                await m.async_poll("camera.a")
            except Exception:
                pass
            assert st.result.label == "none" and st.stale
            assert m.is_detected(st) in (False, None)
    assert m.is_detected(st) is None  # unavailable after 3 failures


async def test_entry_setup_creates_entities(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    e = MockConfigEntry(domain=DOMAIN, data={"api_key": "k", "cameras": ["camera.a"],
                        "interval": 30, "threshold": 0.7, "max_failures": 3})
    e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "analyze")
    assert len(hass.states.async_entity_ids("sensor")) == 1
    bs = hass.states.async_entity_ids("binary_sensor")
    assert len(bs) == 2 and all(hass.states.get(b).state == "unavailable" for b in bs)
    assert await hass.config_entries.async_unload(e.entry_id)


async def test_auth_error_starts_reauth(hass):
    from custom_components.nkd_pet_recognition.api_client import PetApiAuthError
    called = []
    client = MagicMock()
    client.analyze = AsyncMock(side_effect=PetApiAuthError("authentication rejected"))
    m = PetRecognitionManager(hass, "e", client, ["camera.a"], 30, 0.7, 3,
                              on_auth_failed=lambda: called.append(1))
    img = MagicMock(content=b"x", content_type="image/jpeg")
    with patch("custom_components.nkd_pet_recognition.coordinator.async_get_image",
               AsyncMock(return_value=img)):
        try:
            await m.async_poll("camera.a")
        except Exception:
            pass
    assert called == [1]


async def test_reauth_flow(hass, aioclient_mock):
    from custom_components.nkd_pet_recognition import api_client as _a
    aioclient_mock.post(_a.API_URL, status=400, json={"ok": False, "error": {"code": "image_required"}})
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    e = MockConfigEntry(domain=DOMAIN, data={"api_key": "old", "cameras": [],
                        "interval": 30, "threshold": 0.7, "max_failures": 3})
    e.add_to_hass(hass)
    r = await e.start_reauth_flow(hass)
    assert r["step_id"] == "reauth_confirm"
    with patch("custom_components.nkd_pet_recognition.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"api_key": "new"})
    assert r["type"] == FlowResultType.ABORT and r["reason"] == "reauth_successful"
    assert e.data["api_key"] == "new"


# ---- v3 API behaviour -----------------------------------------------------
import io

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from PIL import Image

from custom_components.nkd_pet_recognition import api_client as api
from custom_components.nkd_pet_recognition.api_client import (
    PetApiAuthError,
    PetApiRequestError,
    PetRecognitionClient,
)


def _jpeg(w=300, h=300):
    b = io.BytesIO()
    Image.new("RGB", (w, h)).save(b, "JPEG")
    return b.getvalue()


def _client(hass):
    return PetRecognitionClient(async_get_clientsession(hass), "NKD-DEV-secret")


def _ok(species, score, animals=None, **extra):
    data = {"success": True, "species": species, "species_score": score,
            "animals": animals if animals is not None else
            ([{"species": species, "score": score}] if species != "none" else []),
            "breed": None, "breeds": [], "models_agree": None}
    data.update(extra)
    return {"ok": True, "data": data, "usage": {}}


def _mgr(hass, res=None):
    m = PetRecognitionManager(hass, "e", MagicMock(), ["camera.a"], 30, 0.6, 3)
    if res is not None:
        m.cameras["camera.a"].result = res
    return m, m.cameras["camera.a"]


async def test_200_none_is_good_poll_both_off(hass, aioclient_mock):
    aioclient_mock.post(api.API_URL, json=_ok("none", 0.0))
    res = await _client(hass).analyze(_jpeg())
    assert res.label == "none" and res.confidence == 0.0 and res.animals == []

    # manager: clears stale + failure count, both binary sensors OFF (not unavailable)
    client = MagicMock()
    client.analyze = AsyncMock(side_effect=[PetApiTransientError("engine_error"), res])
    m = PetRecognitionManager(hass, "e", client, ["camera.a"], 30, 0.6, 3)
    st = m.cameras["camera.a"]
    img = MagicMock(content=b"x", content_type="image/jpeg")
    with patch("custom_components.nkd_pet_recognition.coordinator.async_get_image",
               AsyncMock(return_value=img)):
        with pytest.raises(Exception):
            await m.async_poll("camera.a")
        assert st.stale and st.failures == 1
        await m.async_poll("camera.a")
    assert not st.stale and st.failures == 0
    assert m.is_detected(st, "dog") is False and m.is_detected(st, "cat") is False


async def test_404_is_treated_as_none(hass, aioclient_mock):
    aioclient_mock.post(api.API_URL, status=404,
                        json={"ok": False, "error": {"code": "no_dog_detected"}})
    res = await _client(hass).analyze(_jpeg())
    assert res.label == "none" and res.confidence == 0.0
    m, st = _mgr(hass, res)
    assert m.is_detected(st, "dog") is False and m.is_detected(st, "cat") is False


async def test_200_cat_cat_on_dog_off(hass, aioclient_mock):
    aioclient_mock.post(api.API_URL, json=_ok("cat", 0.905), headers={"X-Devapi-Remaining": "99"})
    res = await _client(hass).analyze(_jpeg())
    assert res.label == "cat" and res.confidence == 0.905
    assert res.animals == [{"species": "cat", "score": 0.905}]
    assert res.breed is None and res.breeds == [] and res.usage == {"remaining": "99"}
    m, st = _mgr(hass, res)
    assert m.is_detected(st, "cat") is True
    assert m.is_detected(st, "dog") is False


async def test_200_dog_parsed_threshold_on_species_score(hass, aioclient_mock):
    aioclient_mock.post(api.API_URL, json=_ok(
        "dog", 0.889,
        breed={"id": "labrador", "name": "Labrador Retriever", "score": 0.09},
        breeds=[{"id": "labrador", "name": "Labrador Retriever", "score": 0.09}],
        models_agree=True), headers={"X-Devapi-Remaining": "99"})
    res = await _client(hass).analyze(_jpeg())
    assert res.label == "dog" and res.confidence == 0.889
    assert res.breed == "Labrador Retriever" and res.models_agree is True
    m, st = _mgr(hass, res)
    assert m.is_detected(st, "dog") is True  # low breed score does not matter
    assert m.is_detected(st, "cat") is False


async def test_dog_and_cat_in_one_photo_both_on(hass, aioclient_mock):
    aioclient_mock.post(api.API_URL, json=_ok(
        "dog", 0.93, animals=[{"species": "cat", "score": 0.71}, {"species": "dog", "score": 0.93}]))
    res = await _client(hass).analyze(_jpeg())
    assert [a["species"] for a in res.animals] == ["dog", "cat"]  # best first
    m, st = _mgr(hass, res)
    assert m.is_detected(st, "dog") is True and m.is_detected(st, "cat") is True
    # cat below threshold stays OFF while the dog is ON
    res2 = api._parse_ok(_ok("dog", 0.93, animals=[
        {"species": "dog", "score": 0.93}, {"species": "cat", "score": 0.3}]), {})
    st.result = res2
    assert m.is_detected(st, "dog") is True and m.is_detected(st, "cat") is False


async def test_event_carries_species(hass):
    m, st = _mgr(hass)
    events = []
    hass.bus.async_listen("nkd_pet_recognition_detected", lambda e: events.append(e.data))
    m._on_success(st, PetResult("cat", 0.9, [{"species": "cat", "score": 0.9}]))
    m._on_success(st, PetResult("none", 0.0, []))
    await hass.async_block_till_done()
    assert len(events) == 1 and events[0]["species"] == "cat" and events[0]["label"] == "cat"
    assert events[0]["animals"] == [{"species": "cat", "score": 0.9}]


async def test_errors_keep_failsafe_for_cat_and_dog(hass):
    m, st = _mgr(hass, PetResult("cat", 0.9, [{"species": "cat", "score": 0.9}]))
    for i in range(3):
        m._on_failure(st, PetApiTransientError("timeout"))
        assert st.stale and st.result.label == "cat"  # last good kept, never ON for dog
        assert m.is_detected(st, "dog") in (False, None)
    assert m.is_detected(st, "cat") is None and m.is_detected(st, "dog") is None


async def test_entry_has_dog_and_cat_binary_sensors_with_ids(hass):
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    e = MockConfigEntry(domain=DOMAIN, data={"api_key": "k", "cameras": ["camera.a"],
                        "interval": 30, "threshold": 0.7, "max_failures": 3})
    e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    reg = er.async_get(hass)
    uids = {x.unique_id for x in reg.entities.values() if x.domain == "binary_sensor"}
    assert uids == {f"{e.entry_id}_camera.a_detected", f"{e.entry_id}_camera.a_cat_detected"}
    await hass.config_entries.async_unload(e.entry_id)


async def test_503_retry_after_honoured(hass, aioclient_mock):
    aioclient_mock.post(api.API_URL, status=503, headers={"Retry-After": "300"},
                        json={"ok": False, "error": {"code": "engine_unavailable"}})
    with pytest.raises(PetApiTransientError) as ei:
        await _client(hass).analyze(_jpeg())
    assert ei.value.retry_after == 300
    m = PetRecognitionManager(hass, "e", MagicMock(), ["camera.a"], 30, 0.6, 3)
    assert m._on_failure(m.cameras["camera.a"], ei.value) >= 300
    assert m.cameras["camera.a"].last_error == "engine_unavailable"


async def test_403_triggers_reauth(hass, aioclient_mock):
    aioclient_mock.post(api.API_URL, status=403,
                        json={"ok": False, "error": {"code": "invalid_api_key"}})
    called = []
    m = PetRecognitionManager(hass, "e", _client(hass), ["camera.a"], 30, 0.6, 3,
                              on_auth_failed=lambda: called.append(1))
    img = MagicMock(content=_jpeg(), content_type="image/jpeg")
    with patch("custom_components.nkd_pet_recognition.coordinator.async_get_image",
               AsyncMock(return_value=img)):
        with pytest.raises(Exception):
            await m.async_poll("camera.a")
    assert called == [1]


async def test_400_413_failures_and_small_image_precheck(hass, aioclient_mock):
    aioclient_mock.post(api.API_URL, status=413, json={"ok": False, "error": {"code": "too_big"}})
    with pytest.raises(PetApiRequestError):
        await _client(hass).analyze(_jpeg())
    calls = aioclient_mock.call_count
    with pytest.raises(PetApiRequestError, match="image_too_small"):
        await _client(hass).analyze(_jpeg(150, 400))
    assert aioclient_mock.call_count == calls  # no API call for the small image


async def test_validate_key(hass, aioclient_mock):
    s = async_get_clientsession(hass)
    aioclient_mock.post(api.API_URL, status=403, json={"ok": False, "error": {"code": "invalid_api_key"}})
    with pytest.raises(PetApiAuthError):
        await api.async_validate_key(s, "bad")
    aioclient_mock.clear_requests()
    aioclient_mock.post(api.API_URL, status=400, json={"ok": False, "error": {"code": "image_required"}})
    await api.async_validate_key(s, "good")


async def test_200_cat_breeds_kept(hass, aioclient_mock):
    """v3 cat breeds (0.2.1): breed + top-3 are passed through for cats."""
    top3 = [{"id": "maine-coon", "name": "Maine Coon", "score": 0.51},
            {"id": "abyssinian", "name": "Abyssinian", "score": 0.13},
            {"id": "british-shorthair", "name": "British Shorthair", "score": 0.11}]
    aioclient_mock.post(api.API_URL, json=_ok("cat", 0.905, breed=top3[0], breeds=top3))
    res = await _client(hass).analyze(_jpeg())
    assert res.label == "cat" and res.breed == "Maine Coon"
    assert [b["id"] for b in res.breeds] == ["maine-coon", "abyssinian", "british-shorthair"]


async def test_200_cat_not_confident_breed_none(hass, aioclient_mock):
    top3 = [{"id": "abyssinian", "name": "Abyssinian", "score": 0.48}]
    aioclient_mock.post(api.API_URL, json=_ok("cat", 0.9, breed=None, breeds=top3,
                                              breed_unknown_reason="not_confident"))
    res = await _client(hass).analyze(_jpeg())
    assert res.breed is None and res.breeds[0]["id"] == "abyssinian"
