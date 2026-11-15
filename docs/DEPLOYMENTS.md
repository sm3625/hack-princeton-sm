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
| `southern_cardamom` | 0 (ridge cloud, monsoon gusts) | 3, 60 min | lora |
| `gunung_leuser` | 1 (14 m/s, 16 km range) | 2, 40 min | cellular |

## Maps

A profile's `map:` picks the world, scenarios and OpenStreetMap files (`common/sites.py`):

| Map | Where | World / scenarios | OSM files |
|---|---|---|---|
| `laguna_del_tigre` (default) | Maya Biosphere Reserve, Guatemala | `scenarios/` | `ui/geo/` |
| `southern_cardamom` | Peam Piphot valley above Chi Phat, Cambodia | `sites/southern_cardamom/` | `ui/geo/southern_cardamom/` |
| `gunung_leuser` | Bukit Lawang edge of Gunung Leuser NP, Sumatra | `sites/gunung_leuser/` | `ui/geo/gunung_leuser/` |

```
DEPLOYMENT=gunung_leuser make dev
DEPLOYMENT=southern_cardamom make dev
```

Or pick a map when the dashboard opens: a chooser shows the three sites side by side with each
one's resources (reopen it from the site name under "Forest Guardian"). Switching ends the
current run, points the node pipeline and receiver at the new map's sensors, starts a fresh
commander on that map's profile (`laguna_del_tigre_pilot`, `southern_cardamom`, `gunung_leuser`)
and reloads every open page. `GET /api/sites` lists maps; `PUT /api/sites {"map": ...}` switches
(only when demo run resets are on, the default). A restart goes back to `DEPLOYMENT`.

A world file can describe the site's geography. All optional; the default map has none, so it
behaves as before:

```yaml
site:          {title, region, block, inset_country, inset_zone, osm: {reserve_id, park_id, places},
                research_queries, research_languages, research_hint}
ranger_speed_kmh: 3.0
ranger_posts:  [{name, x_m, y_m, mode: foot|boat|vehicle, speed_kmh}]
terrain:       [{sectors, kind: steep|ridge|river|swamp, note, ranger_factor}]
zones:         [{name, sectors, kind, note, expect: {sound: "HH:MM-HH:MM" or null}}]
```

The commander gets these as "Site notes" in `get_incident_evidence`: terrain at the incident, every
activity zone a reporting node sits in (with whether each heard sound is normal there at this hour,
and whether the sound was also heard outside the zone), and the nearest ranger post with a
terrain-adjusted travel time. `compare_response_options` returns `ranger_eta`; a dispatch records
the post and ETA. The map shades terrain, outlines zones and marks posts (labels on zoom).

| Site | Plays to its strengths | Works around |
|---|---|---|
| Southern Cardamom | Two posts in the block; river boat access to the valley; three teams; quick re-checks | No drone: every check is on foot, and steep ridges double travel time; windward-slope monsoon false alarms |
| Gunung Leuser | Road and phone signal at the boundary; long-range drone over flat lowland | Plantation chainsaws and trucks are lawful by day on the farm side, so the same sound means different things by side and hour; steep poaching hills; village and feeding area no-fly |

Node, post and terrain names are illustrative. OSM coverage in the Cardamom block is sparse.
Refresh geography with `uv run python scripts/fetch_osm.py --site <map>`.

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
