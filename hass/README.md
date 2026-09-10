# Home Assistant integration

Two directions. Home Assistant sends what it can observe; the tracker sends
back one line telling you what to do.

## Setup

1. **Mint a token.** On the tracker's Settings page, create an API token. It is
   shown once.

2. **Add it to `secrets.yaml`**, including the word `Bearer`:

   ```yaml
   hydration_auth: "Bearer xxxxxxxxxxxxxxxxxxxxxxxx"
   ```

3. **Enable packages** in `configuration.yaml` if you have not already:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

4. **Copy `packages/hydration.yaml`** into your `config/packages/` directory.

5. **Edit three things** in it, all marked with `REPLACE`:
   - `input_text.hydration_host` — the tracker's base URL on your tailnet.
   - `rest:` → `resource:` — the same host, with `/api/v1/status` on the end.
   - the two entity ids in the *push ambient conditions* automation — your own
     temperature and humidity sensors.

6. **Restart Home Assistant**, then check `sensor.hydration_plan` has a
   sentence in it rather than `unknown`.

## What you get

| Entity | What it holds |
|---|---|
| `sensor.hydration_plan` | The actionable line, e.g. *"Drink 0.35 L now, then 0.25 L every 30 min until 4:15 PM. Add 500 mg sodium."* Everything else is on it as attributes. |
| `sensor.hydration_deficit` | Litres behind, as a number you can graph. |
| `sensor.hydration_status` | `ok` / `drink` / `drink_urgent` / `add_sodium` / `slow_down` / `unknown` — for card colours and automation triggers. `unknown` means there is not enough recent data to answer; check the `confidence` attribute. |
| `sensor.hydration_drunk_today` | Litres so far today. |
| `sensor.hydration_sodium_gap` | Sweat sodium not yet replaced, in mg. |

## A dashboard card

```yaml
type: vertical-stack
cards:
  - type: markdown
    content: |
      ## {{ states('sensor.hydration_plan') }}

      {{ state_attr('sensor.hydration_plan', 'deficit_l') }} L behind ·
      {{ state_attr('sensor.hydration_plan', 'daily_pct') }}% of today's target
      {% for flag in state_attr('sensor.hydration_plan', 'flags') or [] %}

      ⚠️ {{ flag }}
      {% endfor %}

  - type: entities
    entities:
      - entity: input_select.hydration_beverage
      - entity: input_number.hydration_volume_l
      - type: call-service
        name: Log drink
        icon: mdi:cup-water
        service: script.hydration_submit_drink
      - type: divider
      - entity: input_select.hydration_urine_colour
      - type: call-service
        name: Log void
        icon: mdi:toilet
        service: script.hydration_submit_void
```

## NFC tags

The lowest-friction way to log anything. Write a tag with the Home Assistant
companion app, then:

```yaml
automation:
  - alias: Hydration - fridge tag
    trigger:
      - platform: tag
        tag_id: YOUR_TAG_ID
    action:
      - service: script.hydration_quick_water
```

One on the fridge for water, one in the bathroom that opens a dashboard with
the colour picker on it. `script.hydration_quick_water` is already in the
package for exactly this.

## Sending a body weight

Body weight comes from Garmin automatically if you have an Index scale. If your
scale is in Home Assistant instead, send it:

```yaml
  - alias: Hydration - push morning weight
    trigger:
      - platform: state
        entity_id: sensor.bathroom_scale_weight
    action:
      - service: rest_command.hydration_log_weight
        data:
          mass_lb: "{{ states('sensor.bathroom_scale_weight') | float }}"
          context: morning
```

Weight is the strongest reading the model has, so this is worth wiring up.

## Checking it by hand

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
     http://windows-pc.your-tailnet.ts.net:8080/api/v1/status

curl -X POST -H "Authorization: Bearer YOUR_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"beverage":"Water","volume_l":0.5}' \
     http://windows-pc.your-tailnet.ts.net:8080/api/v1/intake
```

A `401` means the token is wrong or missing the `Bearer ` prefix. A `400`
returns a sentence saying what was wrong with the payload.

## The full API

All routes take `Authorization: Bearer <token>` and accept JSON or form
encoding. Units can be given the way you think in them — `volume_l`, `temp_f`,
`mass_lb` — or metric with `_ml`, `_c`, `_kg`.

| Route | Body |
|---|---|
| `POST /api/v1/intake` | `beverage`, `volume_l`, optional `sodium_mg`, `at`, `note` |
| `POST /api/v1/void` | `colour` (1–8), optional `volume_l`, `urgency`, `at` |
| `POST /api/v1/weight` | `mass_lb`, optional `context`, `at` |
| `POST /api/v1/env` | `temp_f`, `humidity`, optional `location`, `at` |
| `POST /api/v1/symptom` | `kind`, optional `severity`, `at` |
| `POST /api/v1/meal` | optional `label`, `water_l`, `sodium_mg`, `at` |
| `POST /api/v1/feel` | `verdict`: one of `waterlogged`, `a_bit_much`, `about_right`, `a_bit_dry`, `very_dry` |
| `GET /api/v1/status` | — returns the plan |
| `GET /api/v1/beverages` | — the catalogue, for building a dropdown |
