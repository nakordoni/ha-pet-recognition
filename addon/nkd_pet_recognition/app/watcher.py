"""Poll cameras, call the pet-recognition API (dogs and cats), publish over MQTT discovery.

Safety: errors never change the last good result and never turn the binary
sensors ON; the binary sensors go unavailable after max_failures failed polls.
No secrets in logs: API key, MQTT password, snapshot URLs (may hold
credentials) and the Supervisor token are never printed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
import paho.mqtt.client as mqtt

from api_client import (
    LABEL_CAT,
    LABEL_DOG,
    LABEL_NONE,
    PetApiAuthError,
    PetApiError,
    PetRecognitionClient,
    PetResult,
)

LOG = logging.getLogger("nkd_pet")
OPTIONS_PATH = os.environ.get("NKD_OPTIONS", "/data/options.json")
SUPERVISOR = "http://supervisor"
# Some snapshot servers answer 403 to requests without a User-Agent.
SNAPSHOT_UA = "nkd-pet-recognition-addon/0.2.1"
MAX_BACKOFF = 900
BASE = "nkd_pet_recognition"
BRIDGE_TOPIC = f"{BASE}/bridge"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "camera"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Camera:
    name: str
    slug: str
    entity: str | None
    url: str | None
    result: PetResult | None = None
    last_success: str | None = None
    last_error: str | None = None
    stale: bool = False
    failures: int = 0


class Watcher:
    def __init__(self, opts: dict) -> None:
        self.opts = opts
        self.interval = int(opts.get("interval", 30))
        self.threshold = float(opts.get("threshold", 0.6))
        self.max_failures = int(opts.get("max_failures", 3))
        self.cameras: list[Camera] = []
        seen: set[str] = set()
        for c in opts.get("cameras", []):
            slug = slugify(c["name"])
            while slug in seen:
                slug += "_"
            seen.add(slug)
            self.cameras.append(
                Camera(c["name"], slug, c.get("camera_entity") or None, c.get("snapshot_url") or None)
            )
        self.prefix = (opts.get("mqtt", {}).get("discovery_prefix") or "homeassistant").strip("/")
        self.mq: mqtt.Client | None = None
        self.stop = asyncio.Event()

    # ---- MQTT ----------------------------------------------------------
    async def mqtt_conf(self, session: aiohttp.ClientSession) -> dict:
        m = dict(self.opts.get("mqtt", {}))
        if m.get("host"):
            return m
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        try:
            async with session.get(
                f"{SUPERVISOR}/services/mqtt",
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = (await r.json())["data"]
            return {
                "host": data["host"],
                "port": data["port"],
                "username": data.get("username", ""),
                "password": data.get("password", ""),
            }
        except Exception as err:  # noqa: BLE001
            raise SystemExit(f"No MQTT broker configured or discoverable ({type(err).__name__})")

    def connect_mqtt(self, conf: dict) -> None:
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=BASE)
        except AttributeError:  # paho 1.x
            client = mqtt.Client(client_id=BASE)
        if conf.get("username"):
            client.username_pw_set(conf["username"], conf.get("password") or None)
        client.will_set(BRIDGE_TOPIC, "offline", retain=True)
        client.on_connect = lambda *a, **k: self.on_connect()
        client.connect_async(conf["host"], int(conf.get("port") or 1883), keepalive=60)
        client.loop_start()
        self.mq = client

    def on_connect(self) -> None:
        LOG.info("MQTT connected")
        assert self.mq
        self.mq.publish(BRIDGE_TOPIC, "online", retain=True)
        for cam in self.cameras:
            self.publish_discovery(cam)
            self.publish_state(cam)

    def pub(self, topic: str, payload: str) -> None:
        if self.mq:
            self.mq.publish(topic, payload, retain=True)

    def publish_discovery(self, cam: Camera) -> None:
        dev = {
            "identifiers": [f"{BASE}_{cam.slug}"],
            "name": f"Pet recognition {cam.name}",
            "manufacturer": "nakordoni",
        }
        t = f"{BASE}/{cam.slug}"
        bridge = {"topic": BRIDGE_TOPIC}
        self.pub(
            f"{self.prefix}/sensor/{BASE}_{cam.slug}/label/config",
            json.dumps({
                "name": "Animal",
                "unique_id": f"{BASE}_{cam.slug}_label",
                "device": dev,
                "state_topic": f"{t}/label",
                "json_attributes_topic": f"{t}/attributes",
                "availability": [bridge],
                "icon": "mdi:paw",
            }),
        )
        # Dog keeps the original object id / unique_id / topics (existing installs).
        for species, obj, uid, name, topic in (
            (LABEL_DOG, "detected", "detected", "Dog detected", "detected"),
            (LABEL_CAT, "cat_detected", "cat_detected", "Cat detected", "cat_detected"),
        ):
            self.pub(
                f"{self.prefix}/binary_sensor/{BASE}_{cam.slug}/{obj}/config",
                json.dumps({
                    "name": name,
                    "unique_id": f"{BASE}_{cam.slug}_{uid}",
                    "device": dev,
                    "state_topic": f"{t}/{topic}",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "json_attributes_topic": f"{t}/attributes",
                    "availability": [bridge, {"topic": f"{t}/{topic}_availability"}],
                    "availability_mode": "all",
                    "icon": "mdi:dog" if species == LABEL_DOG else "mdi:cat",
                }),
            )

        self.pub(
            f"{self.prefix}/event/{BASE}_{cam.slug}/detected/config",
            json.dumps({
                "name": "Pet detection event",
                "unique_id": f"{BASE}_{cam.slug}_event",
                "device": dev,
                "state_topic": f"{t}/event",
                "event_types": ["detected"],
                "availability": [bridge],
                "icon": "mdi:paw",
            }),
        )

    def fire_event(self, cam: Camera) -> None:
        """Same rule as the integration event: every good poll, non-none label, conf >= threshold."""
        r = cam.result
        if self.mq and r and r.label != LABEL_NONE and r.confidence >= self.threshold:
            self.mq.publish(  # not retained: an event must not replay on restart
                f"{BASE}/{cam.slug}/event",
                json.dumps({"event_type": "detected", "camera": cam.name,
                            "label": r.label, "species": r.label,
                            "confidence": r.confidence, "animals": r.animals}),
            )

    def is_detected(self, cam: Camera, species: str = LABEL_DOG) -> bool | None:
        """None = unavailable. Derived from last good result only."""
        if cam.result is None or cam.failures >= self.max_failures:
            return None
        return cam.result.score(species) >= self.threshold

    def publish_state(self, cam: Camera) -> None:
        t = f"{BASE}/{cam.slug}"
        r = cam.result
        self.pub(f"{t}/attributes", json.dumps({
            "confidence": r.confidence if r else None,
            "species": r.label if r else None,
            "animals": r.animals if r else [],
            "breed": r.breed if r else None,
            "breeds": r.breeds if r else [],
            "models_agree": r.models_agree if r else None,
            "quota": r.usage if r else {},
            "last_success": cam.last_success,
            "last_error": cam.last_error,
            "stale": cam.stale,
            "consecutive_failures": cam.failures,
            "threshold": self.threshold,
        }))
        if r:
            self.pub(f"{t}/label", r.label)
        for species, topic in ((LABEL_DOG, "detected"), (LABEL_CAT, "cat_detected")):
            det = self.is_detected(cam, species)
            if det is not None:
                self.pub(f"{t}/{topic}", "ON" if det else "OFF")
            self.pub(f"{t}/{topic}_availability", "offline" if det is None else "online")

    # ---- fetch ---------------------------------------------------------
    async def fetch_image(self, session: aiohttp.ClientSession, cam: Camera) -> tuple[bytes, str]:
        if cam.entity:
            token = os.environ.get("SUPERVISOR_TOKEN", "")
            url = f"{SUPERVISOR}/core/api/camera_proxy/{cam.entity}"
            headers = {"Authorization": f"Bearer {token}"}
        elif cam.url:
            url, headers = cam.url, {"User-Agent": SNAPSHOT_UA}
        else:
            raise RuntimeError("camera has neither camera_entity nor snapshot_url")
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                raise RuntimeError(f"snapshot HTTP {r.status}")
            return await r.read(), r.headers.get("Content-Type", "image/jpeg").split(";")[0]

    # ---- loop ----------------------------------------------------------
    async def poll_once(self, session, client: PetRecognitionClient, cam: Camera) -> float:
        delay = float(self.interval)
        try:
            image, ctype = await self.fetch_image(session, cam)
            res = await client.analyze(image, ctype)
        except Exception as err:  # noqa: BLE001 - any error is a failed poll
            cam.failures += 1
            cam.stale = True  # last good result kept untouched
            if isinstance(err, PetApiError):
                cam.last_error = str(err)
            elif isinstance(err, RuntimeError):
                cam.last_error = f"snapshot: {err}"
            else:
                cam.last_error = f"snapshot: {type(err).__name__}"
            LOG.warning(
                "Poll failed for %s (%s), consecutive failures: %d",
                cam.slug, cam.last_error, cam.failures,
            )
            backoff = (
                MAX_BACKOFF
                if isinstance(err, PetApiAuthError)
                else min(self.interval * 2 ** cam.failures, MAX_BACKOFF)
            )
            delay = max(float(self.interval), backoff, float(getattr(err, "retry_after", None) or 0))
        else:
            cam.result, cam.last_success = res, now_iso()
            cam.last_error, cam.stale, cam.failures = None, False, 0
            self.publish_state(cam)
            self.fire_event(cam)
            return delay
        self.publish_state(cam)
        return delay

    async def camera_loop(self, session, client, cam: Camera) -> None:
        while not self.stop.is_set():
            delay = await self.poll_once(session, client, cam)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        if not self.opts.get("api_key"):
            raise SystemExit("api_key option is empty")
        if not self.cameras:
            raise SystemExit("No cameras configured")
        async with aiohttp.ClientSession() as session:
            self.connect_mqtt(await self.mqtt_conf(session))
            client = PetRecognitionClient(session, self.opts["api_key"])
            await asyncio.gather(*(self.camera_loop(session, client, c) for c in self.cameras))
        if self.mq:
            self.mq.publish(BRIDGE_TOPIC, "offline", retain=True).wait_for_publish(2)
            self.mq.loop_stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(OPTIONS_PATH, encoding="utf-8") as fh:
        opts = json.load(fh)
    w = Watcher(opts)

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, w.stop.set)
        await w.run()

    asyncio.run(runner())


if __name__ == "__main__":
    main()
