# NKD Pet Recognition add-on

**Detects dogs and cats. Breeds: 60 dog breeds and 12 pedigree cat breeds (a mixed-breed cat usually shows no breed).** No animal shows as `none`.

Polls camera snapshots, sends them to the nakordoni pet-recognition API (POST, multipart `image`) and
publishes the result over MQTT discovery (sensor, dog and cat binary sensors and event per camera).

## Options
- `api_key` (password): DevAPI key. Stored only here.
- `cameras`: list of `name` plus EITHER `camera_entity` (fetched through the Supervisor API) OR `snapshot_url`.
- `interval`: seconds between analyses per camera (default 30).
- `threshold`: species score needed for "Dog detected" / "Cat detected" (default 0.6). Applies to the species score only; breed scores are low by design and never thresholded.
- `max_failures`: failed polls in a row before the binary sensors are `unavailable` (default 3).
- `mqtt`: leave `host` empty to use the Supervisor Mosquitto service.

## Entities per camera
- `sensor` "Animal": state is `dog`, `cat` or `none`. Attributes: `species`, `confidence` (species_score), `animals` (`{species, score}`, best first), `breed` (name, dogs only), `breeds` (top 3, dogs only), `models_agree`, `quota`, `last_success`, `last_error`, `stale`, `consecutive_failures`.
- `binary_sensor` "Dog detected" (unique_id unchanged): ON when a dog is in `animals` with score >= threshold.
- `binary_sensor` "Cat detected" (new, unique_id `..._cat_detected`): ON when a cat is in `animals` with score >= threshold.
- `event` "Pet detection event" (`event_types: [detected]`): fired on every good poll whose label is `dog` or `cat` and confidence >= threshold, same rule as the integration event. Event attributes: `camera`, `label`, `species`, `confidence`, `animals`. It repeats each interval while a pet stays in view, so automations should debounce.

## Failure behaviour
No animal is HTTP 200 with `species: none` (a 404 is also treated as `none`): a good poll, label `none`, not stale, failure count reset.
Images with a shorter side under 200 px are not sent: failed poll with `last_error: image_too_small`.
On HTTP 429/500/503, timeout or network error the add-on honours `Retry-After`, backs off
exponentially (max 15 min), keeps the LAST GOOD state and sets `stale: true`.
An error never turns a binary sensor ON. After `max_failures` failed polls both
become `unavailable`.

## Pet doors
The result is a classification with a confidence score (dogs and cats). The door controller must keep its own
safe default. Do not rely on this for animal safety. See the repository README for an example automation.

## Logs
No API key, MQTT password, snapshot URL or token is ever logged.
