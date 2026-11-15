# Incident-aware public research

The commander can request public context while continuing an investigation. Research runs on
its own bounded worker and returns a distinct `research_ready` wake; it never occupies the
commander executor while waiting on the network. Pending approvals defer that wake, and a
completed or dispatched incident is not automatically reopened.

## Configuration and deployment

Set `RESEARCH_ENABLED=true` and `TAVILY_API_KEY` in the station's environment. Keep the key out
of source control and the browser. `RESEARCH_DB` defaults to `research.sqlite` at the repository
root; give the service user write access. The SQLite cache is separate from disposable demo
checkpoints. The feature defaults to disabled and does not require a key in that mode.

Install dependencies with `uv sync --frozen`. Restart the persistent station service after
changing environment variables. The Vercel entry-point redirect does not run this worker.
To roll back, set `RESEARCH_ENABLED=false` and restart; existing incident evidence is unchanged.

The model follows the station's selected provider. A provider switch applies to new research
jobs; already queued work completes using its original model. No browser installation or
Agent Protocol server is required. No browser automation, shell, ranger, or dispatch tools are
exposed to the researcher.

## Data and lifecycle

`request_site_research(incident_id, question)` returns immediately with status and cached
context. The question is an indication that context is needed; v1 sends only a standardized
research question, the code-owned threat category, approximate public site centre, and site
description to the researcher. Free-form questions, ranger messages and detection records
are not sent to search providers. Search queries are generated from that public context.
`get_site_research(incident_id)` reads findings and their source metadata without raw excerpts.

A small public source pack is gathered first, including a real-world forecast and two focused
searches. The researcher can follow up within the same overall budget. A final bounded
submission step prevents a model that keeps searching from silently dropping its findings.
Gemini research uses the actual `max_output_tokens` field and disables thinking for this
latency-bounded role; the commander configuration is unchanged.

The researcher reads the bundled read-only skill and uses explicit todo planning. It can
search at most six times and extract eight pages. Provider calls retry transient failures
once, within the 90-second overall job deadline. One worker has at most eight waiting jobs.
Equivalent site/topic jobs share results. Reset/retirement cancels in-flight async work and
revokes callbacks; the new run cannot receive an old result. Interrupted attachment states
are marked failed on process restart; the next request may retry.

Narrative findings expire after seven days; forecast findings after one hour. Fresh narrative
is retained during forecast refresh. Failed jobs retain the previous briefing. Partial output
is explicitly labeled; duplicate retries from the same incident back off five minutes.
Attachments survive a regular process restart, scoped by commander database path; disposable
demo runs have separate attachment namespaces. Source registry and attachments are written
by code, not model filesystem tools.

`GET /api/incidents/{id}/research` returns status, optional error, briefing, stale labels,
sanitized HTML and assessment context version. Incident and case-file responses also include
`research`. WebSocket `research` events carry the same view. Decision-trail events record
requests, source reads, completion and context versions supplied for reassessment.

## Interpretation limits

All external text is untrusted advisory data. Citations must reference extracted sources;
missing dates, contradictions and uncertainty remain visible. The model may make inferences
but must label them. Citation validation checks source existence, not the truth of a claim.
V1 conservatively treats narrative findings as regional: there is no geospatial adapter to
justify sector rankings or overlays. Public sources cannot establish criminal activity by
people at the site or override a ranger correction.

Regional uncertainty and possible legitimate activities do not cancel corroborated local
reports or a supporting camera observation. The commander may request human approval for a
field check without claiming a crime occurred. It saves its assessment before proposing
dispatch in a separate tool call; Claude commander calls disable parallel tools so an approval
interrupt cannot pause the assessment it depends on. Station validation failures are logged
as validation failures, not as ranger rejections.

Forecasts are for real UTC dates and a weather-model grid near the public site centre. They
are not observations and never replace simulated `get_weather`, change flight limits, queue
priority, sensor confidence or the dispatch approval gate. Seasonal narratives require
separate sources. The forecast alone cannot establish seasonal patterns.

## Verification

Run `.venv/bin/pytest -q tests/test_research.py` for fixture-backed research tests, plus the
existing agent, incident, route and demo-session regressions. With a real Tavily key, trigger
a report, observe Research requested / Sources reviewed / Findings available, and verify
source-linked context and a subsequent reassessment. If approval is pending, context appears
immediately but the agent waits until the human decision resolves. Confirm simulated weather
and existing approval behavior are unchanged.

## Follow-on work (not part of v1)

Evaluate citation support and geography offline before enabling rubric-based automatic
revision. Add reviewed historical incident retrieval, authoritative geospatial alerts and
historical-weather aggregates before sector maps. Interactive browser tooling remains a
separate extension for pages that require rendered navigation.
