# Deployment profiles

The same code runs at sites with different resources. A profile is one YAML file in
`deployments/`, chosen with `DEPLOYMENT` (file name without `.yaml`; default
`laguna_del_tigre_pilot`, which matches the original one-drone behaviour).

```
DEPLOYMENT=no_drone make dev
```

| Profile | Drones | Ranger teams | Link |
|---|---|---|---|
| `laguna_del_tigre_pilot` (default) | 1 (10 m/s, 10 km range, 15% reserve) | 1, 45 min | lora |
| `no_drone` | 0 | 1, 90 min | satellite |
| `well_resourced` | 3 (two bases, mixed specs) | 4, 20 min | cellular |

## Fields

```yaml
site:            {name, description}
drones:
  count: 0..12
  spec:          {speed_mps, range_km, reserve_pct, base_x_m, base_y_m}   # fleet defaults
  units:         [{id, speed_mps, range_km, reserve_pct, base_x_m, base_y_m}]  # optional, by position
ranger_teams:    {count, response_min}
connectivity:    lora | satellite | cellular      # informational: prompt and API only
approval:        {approver}                       # who signs off; the gate itself is not a setting
agent:           {max_followup_checks, check_interval_min}
```

- `range_km` is the out-and-back distance a full battery covers (10 km = 10% battery per km).
- Omit `base_x_m` / `base_y_m` to start at the map base (`scenarios/world.yaml`). Give both or neither.
- Unknown keys and out-of-range values are rejected at start-up with a clear message. There is
  no key to turn the dispatch approval off, and `approval: {required: false}` is an error.

## What changes

- **No drone:** no drone-pilot sub-agent, no drone tools, no drone in the prompts or the seeded
  `plan.md`. The commander dispatches (or asks a ranger to verify) with the note "No aerial
  verification available." The station adds that note itself if the model forgets, and the
  approval card shows it.
- **Several drones:** `plan_route` picks the nearest idle drone with enough battery (`route.drone_id`),
  `fly_drone` flies that one, `get_drone_status` lists all. Drone bus messages and `/api/sim/state`
  keep their old fields and gain `id`; `state.drone` is the first drone, `state.drones` lists all.
- **Prompt:** the commander's system prompt gets one line with ranger teams, response time, link,
  approver and the check interval (and the check cap if set).
- **API:** `GET /api/deployment` returns the active profile, read-only, no secrets.

## Limits

- The UI draws one drone. With 2+ drones it receives one message stream per drone id and
  will flicker between them until the UI learns about ids (not yet done).
- Drones are independent: no handover, and each starts and returns to its own base.
- `max_followup_checks` is counted in memory per incident, so it resets on a restart.
- `connectivity` and `response_min` are informational; the sim does not delay rangers.
- The mock model always schedules 30 minute checks, whatever the profile says.
