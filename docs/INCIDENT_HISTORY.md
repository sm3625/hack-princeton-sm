# Incident history and site playbooks

The commander and triage agent can call `retrieve_incident_history(limit=3)` to retrieve
prior outcomes without loading every incident into the system prompt. Retrieval is deterministic:
shared nodes rank first, followed by matching threat family and sector, then recency. Only
completed/dismissed incidents or those with an explicit ranger outcome are eligible. Results
must predate the current incident and fall within 90 days of simulation/station time. The
current incident, unrelated incidents and future records are excluded; output is capped at five.

Each result includes its incident ID, timestamps, matching criteria, summary, rain context,
and an explicit distinction between ranger reports and agent-only dispositions. Prior approval
or dismissal is not ground truth. Historical records never count as current corroboration.
The commander should cite historical incident IDs when they affect a recommendation; tool
calls also appear in the decision trail.

History comes from the existing SQLite-backed incident repository. It survives reopening the
same station database. Disposable demo resets and site changes intentionally start isolated
histories; this version does not pool old demo runs or share records across deployments.

`agent/site_skills/<map>/SKILL.md` contains maintained playbooks for Laguna del Tigre,
Southern Cardamom and Gunung Leuser. At agent construction, only the active site's playbook
is copied into the virtual filesystem. Deep Agents advertises its description and loads the
full instructions on demand through `read_file`. Filesystem permissions deny writes recursively
to `/skills/**`, including edits from subagents. Edit the source playbook and rebuild the agent
to change it; retrieved text never edits skills automatically.

Local terrain and activity-zone details are identified as illustrative scenario configuration,
not independently verified regulations. Playbooks preserve approval requirements, ranger
corrections and camera uncertainty checks while encouraging timely field-verification requests
when local evidence warrants them. Neither retrieval nor skills adds a blocking review worker.

Validation: `pytest -q tests/test_incident_history.py tests/test_agent_commander.py` covers
retrieval boundaries, persisted history, site selection and rejection of attempted skill writes.
