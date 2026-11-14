# Gemini on Vertex AI using ADC

The commander's four Deep Agents roles can use `ChatGoogleGenerativeAI` through
`langchain-google-genai` and the `google-genai` SDK. Production sound classification is
unchanged and still runs locally / in the selected edge worker.

```sh
uv sync
# After installing Google's gcloud CLI, if ADC is not already configured:
gcloud auth application-default login
```

Set these in the ignored `.env` file:

```dotenv
LLM_MODE=gemini
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=global
GEMINI_MODEL=gemini-3.5-flash
GEMINI_TIMEOUT_S=60
EDGE_BACKEND=local
```

The Google Cloud project must have Vertex AI access, billing and permission to invoke the
selected model. ADC may come from local user credentials, an explicitly configured credentials
file, or a cloud workload identity. The integration explicitly supplies ADC credentials and
`vertexai=True`; it does not require a Gemini/AI Studio API key or silently use another provider.
The project is read from `GOOGLE_CLOUD_PROJECT`, then the project discovered by ADC. A user ADC
quota project alone is not silently assumed to be the deployment project.

Restart the station after changing backend configuration:

```sh
.venv/bin/python -m emulators.demo
```

If port 8000 is already occupied, stop that project's older station first. Browser assets can
update while a non-reloading Python server keeps its old routes: a 404 on `/api/edge/status`
means the running server needs a restart. The edge panel now explains this explicitly.
The health endpoint should show `mode: gemini`, and the edge panel should show **Connected**.

Haiku remains selectable with `LLM_MODE=haiku` for controlled comparisons and rollback.
No model call is made simply by changing the configuration; live agent work invokes the API.
A provider startup error is logged and is not silently replaced by Haiku or a mock.

## Reproduce the comparison

```sh
.venv/bin/python -m eval.compare_cloud --modes haiku gemini --repeats 1
```

This makes paid model calls using your configured credentials. Each provider sees the same
four fixtures: multi-node chainsaw, rainy chainsaw cluster, two-node gunshot, and single-node
rain. Each case has an isolated temporary station, real Deep Agents tools, signed fixture
records, fake clock/drone and a 150-second process timeout. Nothing contacts real rangers.
The test stops at the first decision or approval request; it does not approve a dispatch.

The report measures agent wall time, model steps, decision, invented sensor names, agent
failures and whether any dispatch occurred without approval. `passed` means the decision
matched the existing quick-eval target AND the safety checks passed. The quick-eval fixture's
"fresh clearing, one vehicle" drone observation is ambiguous: conservative `watching` on
positive cases can be reasonable under the current policy. The four targets are not a
validated accuracy benchmark. A single pass cannot establish statistical equivalence.

Use `--repeats 3` for a less noisy comparison; `--output PATH` preserves a separate report.
Missing credentials are recorded as blocked, never as measured zero latency or a model score.
`eval.quick`, `eval.demo`, and `eval.run` also accept `--mode gemini`.

Sources: [LangChain Gemini integration](https://docs.langchain.com/oss/python/integrations/chat/google_generative_ai),
[Google ADC setup](https://cloud.google.com/docs/authentication/set-up-adc-local-dev-environment).
