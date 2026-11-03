"""Constants for NKD Pet Recognition."""
from __future__ import annotations

DOMAIN = "nkd_pet_recognition"

CONF_API_KEY = "api_key"
CONF_CAMERAS = "cameras"
CONF_INTERVAL = "interval"
CONF_THRESHOLD = "threshold"
CONF_MAX_FAILURES = "max_failures"

DEFAULT_INTERVAL = 30  # seconds
MIN_INTERVAL = 5
DEFAULT_THRESHOLD = 0.6  # applies to dog_score only
DEFAULT_MAX_FAILURES = 3

MAX_BACKOFF = 900  # seconds, cap for exponential back-off
MAX_RETRY_AFTER = 3600  # seconds, cap for server supplied Retry-After
IMAGE_TIMEOUT = 10  # seconds, camera snapshot
LABEL_NONE = "none"

EVENT_DETECTED = "nkd_pet_recognition_detected"
SERVICE_ANALYZE = "analyze"
ATTR_CAMERA = "camera"
ATTR_LABEL = "label"
ATTR_CONFIDENCE = "confidence"
