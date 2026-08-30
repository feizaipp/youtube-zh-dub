# Agent text-exchange protocol

The pipeline deliberately does not call a vendor-specific model CLI. At each text stage it may stop after writing a request in:

`<workdir>/segments/agent_text_requests/<fingerprint>.request.json`

The Agent that invoked the Skill must complete that request with its current backend model. Read `system`, `user`, and `output_schema`; follow them exactly. Do not use tools or make network requests while performing the editorial task. The result must be JSON conforming to `output_schema`.

Write the response to the `response_path` named in the request, using this envelope:

```json
{
  "request_fingerprint": "copy from the request",
  "result": {"the JSON value required by output_schema": "..."}
}
```

Then rerun the exact same pipeline command. The request fingerprint prevents a stale response being applied after the transcript, prompt, or schema changes. The pipeline caches accepted results and may stop again for the next batch or an overlong-line rewrite; repeat the same exchange until it finishes.

Never invent or alter segment IDs, timestamps, facts, product names, numbers, or required final punctuation. Return JSON only. If the request cannot be answered reliably, report that limitation instead of writing a fabricated response.
