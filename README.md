# NKD Pet Recognition for Home Assistant

**Detects dogs and cats. Breeds: 60 dog breeds and 12 pedigree cat breeds (a mixed-breed cat usually shows no breed).**

Two packages for the nakordoni DevAPI pet-recognition product:

- `custom_components/nkd_pet_recognition/`: HACS custom integration (config flow, sensor + dog/cat binary sensors per camera, event, service).
- `addon/nkd_pet_recognition/`: Supervisor add-on that publishes the same data over MQTT discovery.

## Get an API key

Both packages need a nakordoni DevAPI key. Sign up at https://nakordoni.eu/en/developers and create a key in the portal. The free Explorer plan includes 20 pet-recognition calls per day, enough for one camera on a slow interval; paid plans raise the limit.

The packages call `POST https://nakordoni.eu/api/v3/data/pet-recognition` (multipart `image`, `Authorization: Bearer <key>`).

## Integration

Set up via Settings, Devices and services, Add integration, "NKD Pet Recognition": API key, cameras, interval, confidence threshold (default 0.6, applied to the species score). The key check posts an empty body: 403 means invalid key, any other answer means the key is fine. This may count against quota if the gateway meters before validation. Options flow also sets "failed polls before unavailable" (default 3).

Per camera:
- `sensor` "Animal": state is `dog`, `cat` or `none`. Attributes: `species`, `confidence` (species_score), `animals` (list of `{species, score}`, best first), `breed` (name, dogs only), `breeds` (top 3, dogs only), `models_agree`, `quota`, `last_success`, `last_error`, `stale`. Breed scores are low by design (0.05 to 0.2) and are never thresholded.
- `binary_sensor` "dog detected" (unique_id `..._detected`, unchanged): ON when a dog is in `animals` with score >= threshold.
- `binary_sensor` "cat detected" (new, unique_id `..._cat_detected`): ON when a cat is in `animals` with score >= threshold. Both can be ON for one photo. Both follow the same fail-safe rules.
- Event `nkd_pet_recognition_detected` with `camera`, `label`, `species`, `confidence`, `animals` on every good poll at or above threshold. It repeats each interval while a pet is in view, so automations should debounce (see below).
- Service `nkd_pet_recognition.analyze` with `entity_id` (camera): one analysis now; returns the result as a response.

### Debouncing the event

Use `for:` on a state trigger, or a condition on the last trigger time:

```yaml
trigger:
  - platform: state
    entity_id: binary_sensor.front_door_cam_cat_detected
    to: "on"
    for: "00:00:03"   # must stay on for 3 s
condition:
  - condition: template   # at most once per minute
    value_template: >
      {{ this.attributes.last_triggered is none
         or (now() - this.attributes.last_triggered).total_seconds() > 60 }}
```

## Reauth

If the API answers 403 `invalid_api_key` (401 is treated the same), Home Assistant shows "re-enter key" (reauth flow) and polling backs off 15 min.

## Failure behaviour

No animal is HTTP 200 with `species: none` (a 404 is also treated as `none`): a good poll, label `none`, not stale, failures reset. Snapshots with a shorter side under 200 px are not sent (failed poll, `last_error: image_too_small`). HTTP 400 and 413 are failed polls. On HTTP 429/500/503, timeout or network error: `Retry-After` is honoured, polling backs off (max 15 min), the LAST GOOD state is kept and marked `stale: true`. An error never turns a binary sensor ON. After N consecutive failed polls (default 3) both binary sensors are `unavailable`, so automations can fail safe. The API key is stored only in the config entry (or add-on options) and is never logged.

## Pet doors

Dogs and cats are detected, other animals look like "none". The result is a classification with a confidence score, not a guarantee. The door controller must keep its own safe default (for example: locked unless told otherwise, with its own timeouts and sensors). Do not rely on this integration for animal safety.

Example: unlock the cat door only when a cat is detected AND the data is fresh (use `dog_detected` the same way for a dog door). `unavailable` and `stale` both keep the door in its safe default.

```yaml
automation:
  - alias: "Cat door: unlock for detected cat"
    mode: single
    trigger:
      - platform: state
        entity_id: binary_sensor.front_door_cam_cat_detected
        to: "on"
    condition:
      - condition: state
        entity_id: binary_sensor.front_door_cam_cat_detected
        state: "on"
      - condition: template
        value_template: >
          {{ is_state_attr('binary_sensor.front_door_cam_cat_detected', 'stale', false) }}
    action:
      - service: lock.unlock
        target:
          entity_id: lock.pet_door
      - delay: "00:00:20"
      - service: lock.lock
        target:
          entity_id: lock.pet_door
```

The re-lock after the delay is a convenience; the door's own controller should also relock on its own.

## Install (HACS)

HACS → ⋮ → Custom repositories → add `https://github.com/nakordoni/ha-pet-recognition` (category: Integration) → install "NKD Pet Recognition" → restart Home Assistant → Settings → Devices & services → Add integration.

## Add-on

Add `https://github.com/nakordoni/ha-pet-recognition` as an add-on repository (Settings → Add-ons → Add-on store → ⋮ → Repositories), install "NKD Pet Recognition", fill options (see `addon/nkd_pet_recognition/DOCS.md`). Entities appear through MQTT discovery; the binary sensors have the same stale and unavailable semantics.
