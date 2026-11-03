"""The ONLY module that talks to the nakordoni DevAPI pet-recognition product.

Spec (v3): POST https://nakordoni.eu/api/v3/data/pet-recognition
The API detects dogs and cats. No animal is a 200 with species "none" (a GOOD
poll, not a failure). A 404 is also treated as "none" for safety.

Secrets: the API key is only placed in the auth header. Never logged.
"""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

LABEL_NONE = "none"
MAX_RETRY_AFTER = 3600

API_URL = "https://nakordoni.eu/api/v3/data/pet-recognition"
AUTH_HEADER_NAME = "Authorization"
AUTH_HEADER_FORMAT = "Bearer {api_key}"
REQUEST_TIMEOUT = 15  # p50 ~0.85 s, p95 ~4.5 s
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MIN_SIDE_PX = 200
LABEL_DOG = "dog"
LABEL_CAT = "cat"


class PetApiError(Exception):
    """Base error. str(err) is stored as last_error; never contains the key."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PetApiTransientError(PetApiError):
    """429, 500, 503, timeout, network error: retry later, keep last good (stale)."""


class PetApiAuthError(PetApiError):
    """403 invalid_api_key (or 401): start reauth."""


class PetApiRequestError(PetApiError):
    """400, 413, other 4xx, bad body, image pre-check: a failed poll."""


@dataclass
class PetResult:
    """Normalised result. label is dog|cat|none; confidence is species_score."""

    label: str
    confidence: float
    animals: list[dict[str, Any]] = field(default_factory=list)  # [{species, score}], best first
    breed: str | None = None  # attribute only, never thresholded
    breeds: list[dict[str, Any]] = field(default_factory=list)  # top 3, attribute only
    models_agree: bool | None = None
    usage: dict[str, Any] = field(default_factory=dict)  # quota headers

    def score(self, species: str) -> float:
        """Best score of one species in this photo (0.0 when absent)."""
        return max(
            (float(a.get("score") or 0.0) for a in self.animals if a.get("species") == species),
            default=0.0,
        )


NO_ANIMAL = PetResult(LABEL_NONE, 0.0, [])


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (seconds or HTTP date). Capped."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            return None
    return max(0.0, min(seconds, float(MAX_RETRY_AFTER)))


def _headers(api_key: str) -> dict[str, str]:
    return {AUTH_HEADER_NAME: AUTH_HEADER_FORMAT.format(api_key=api_key)}


def _usage(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    h = resp.headers
    out = {
        "limit": h.get("X-Devapi-Limit"),
        "remaining": h.get("X-Devapi-Remaining"),
        "reset": h.get("X-Devapi-Reset"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _parse_ok(payload: Any, usage: dict[str, Any]) -> PetResult:
    """200 body: {"ok":true,"data":{"species":"dog|cat|none","species_score":..,
    "animals":[{"species","score"}],"breed":{..}|null,"breeds":[..],"models_agree":bool}}"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not payload.get("ok", False):
        raise PetApiRequestError("unexpected response shape")
    animals = [
        {"species": a.get("species"), "score": float(a.get("score") or 0.0)}
        for a in (data.get("animals") or [])
        if isinstance(a, dict) and a.get("species") in (LABEL_DOG, LABEL_CAT)
    ]
    animals.sort(key=lambda a: a["score"], reverse=True)
    species = data.get("species")
    if species not in (LABEL_DOG, LABEL_CAT):
        return PetResult(LABEL_NONE, 0.0, animals, usage=usage)
    score = float(data.get("species_score") or 0.0)
    if not animals:
        animals = [{"species": species, "score": score}]
    is_dog = species == LABEL_DOG
    breeds = [
        {"id": b.get("id"), "name": b.get("name"), "score": b.get("score")}
        for b in (data.get("breeds") or [])[:3]
        if isinstance(b, dict)
    ]  # v3: dogs (60 breeds) and cats (12 pedigree breeds)
    breed = data.get("breed")  # null when the API is not confident (breed_unknown_reason)
    return PetResult(
        label=species,
        confidence=score,
        animals=animals,
        breed=breed.get("name") if isinstance(breed, dict) else None,
        breeds=breeds,
        models_agree=data.get("models_agree") if is_dog else None,
        usage=usage,
    )


async def _error_code(resp: aiohttp.ClientResponse) -> str | None:
    """error.code from {ok:false,error:{code,...}}, or None."""
    try:
        body = await resp.json(content_type=None)
        code = body["error"]["code"]
        return str(code)[:64] if code else None
    except (ValueError, TypeError, KeyError, aiohttp.ClientError):
        return None


def check_image(image: bytes) -> None:
    """Pre-check size and shorter side (>= 200 px). Raises PetApiRequestError.

    Pillow is shipped with Home Assistant; if it is missing the side check is skipped.
    """
    if len(image) > MAX_IMAGE_BYTES:
        raise PetApiRequestError("image_too_large")
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return
    try:
        with Image.open(io.BytesIO(image)) as im:
            if min(im.size) < MIN_SIDE_PX:
                raise PetApiRequestError("image_too_small")
    except PetApiRequestError:
        raise
    except Exception:  # noqa: BLE001 - undecodable header: let the API judge
        return


class PetRecognitionClient:
    """Thin async client."""

    def __init__(self, session: aiohttp.ClientSession, api_key: str) -> None:
        self._session = session
        self._api_key = api_key

    async def analyze(self, image: bytes, content_type: str = "image/jpeg") -> PetResult:
        """Classify one snapshot. Raises PetApiError subclasses only."""
        check_image(image)  # no API call when the image cannot be valid
        data = aiohttp.FormData()
        data.add_field("image", image, content_type=content_type, filename="snapshot.jpg")
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        try:
            async with self._session.post(
                API_URL, data=data, headers=_headers(self._api_key), timeout=timeout
            ) as resp:
                return await self._handle(resp)
        except asyncio.TimeoutError:
            raise PetApiTransientError("timeout") from None
        except aiohttp.ClientError as err:
            # class name only: aiohttp messages may contain URLs
            raise PetApiTransientError(f"network error ({type(err).__name__})") from None

    async def _handle(self, resp: aiohttp.ClientResponse) -> PetResult:
        status = resp.status
        usage = _usage(resp)
        if status == 200:
            try:
                return _parse_ok(await resp.json(content_type=None), usage)
            except PetApiError:
                raise
            except (ValueError, TypeError, aiohttp.ClientError) as err:
                raise PetApiRequestError(f"bad response body ({type(err).__name__})") from None
        code = await _error_code(resp)
        retry_after = parse_retry_after(resp.headers.get("Retry-After"))
        if status == 404:  # v3 answers 200/none; a 404 is still "no animal", not an error
            return PetResult(LABEL_NONE, 0.0, [], usage=usage)
        label = code or f"HTTP {status}"
        if status in (401, 403):
            raise PetApiAuthError(label)  # 403 invalid_api_key
        if status == 429 or status >= 500:  # 429, 500 engine_error, 503 engine_unavailable/busy
            raise PetApiTransientError(label, retry_after)
        raise PetApiRequestError(label, retry_after)  # 400, 413, other 4xx

