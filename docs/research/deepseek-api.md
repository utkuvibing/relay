# DeepSeek API: OpenAI-compatible integration notes

**Checked:** 2026-08-30
**Sources:** official DeepSeek API documentation only.

## Connection details

- **OpenAI-compatible base URL:** `https://api.deepseek.com`
- **Chat Completions endpoint:** `POST https://api.deepseek.com/chat/completions`
- **Authentication:** send `Authorization: Bearer <DEEPSEEK_API_KEY>` and `Content-Type: application/json`. Keep the key in an environment variable; do not put it in Relay configuration or source.
- **OpenAI SDK configuration:** set `base_url`/`baseURL` to `https://api.deepseek.com` and `api_key`/`apiKey` from the environment.

Sources: [Your First API Call](https://api-docs.deepseek.com/guides/reasoning_model_api_example_non_streaming), [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/).

## Current model IDs for a BYOK smoke test

- `deepseek-v4-flash` - recommended smoke-test default: current OpenAI Chat Completions model, supports thinking and non-thinking modes.
- `deepseek-v4-pro` - current higher-capability alternative using the same base URL and endpoint.
- `deepseek-v4-flash-vision-exp` - use only when testing the documented multimodal/vision request shape.

The docs say the model IDs above resolve to the current versions (including `DeepSeek-V4-Flash-0731` and `DeepSeek-V4-Pro-0813`). Do not use `deepseek-chat` or `deepseek-reasoner` for a new smoke test: the official release note says those legacy names became inaccessible after 2026-07-24.

Sources: [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/), [V4 release note](https://api-docs.deepseek.com/news/news260424/).

## Minimal request and response shape

Request body requires a non-empty `messages` array and a `model`; a minimal request is:

```json
{
  "model": "deepseek-v4-flash",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Reply with exactly: smoke test ok"}
  ],
  "stream": false
}
```

The non-streaming response is an OpenAI-shaped chat completion with fields including `id`, `object: "chat.completion"`, `created`, `model`, `choices`, and `usage`. Read the generated text from `choices[0].message.content`; `choices[0].finish_reason` reports why generation ended. `usage` includes prompt, completion, cache-hit/cache-miss, and total token counts.

For streaming, set `stream: true`; the API returns `chat.completion.chunk` events whose generated deltas are under `choices[].delta`.

Source: [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/).

## Compatibility caveats

- "OpenAI-compatible" means the OpenAI SDK/request shape can be used, not that every OpenAI option has identical semantics.
- Thinking mode is enabled by default. To force ordinary non-thinking behavior, send `thinking: {"type": "disabled"}`. With the OpenAI Python SDK, DeepSeek documents passing this non-standard field through `extra_body`; the equivalent raw JSON field is top-level.
- In thinking mode, `temperature`, `top_p`, `presence_penalty`, and `frequency_penalty` have no effect (the API accepts them for compatibility). Reasoning text is returned as `reasoning_content` alongside `content`.
- If a tool-calling conversation uses thinking mode, preserve and send back the complete prior `reasoning_content` on subsequent turns or DeepSeek can return HTTP 400.
- `reasoning_effort` supports `low`, `high`, and `max`; `medium` and `xhigh` are mapped to `high` for compatibility.
- Beta-only features, such as chat-prefix completion and strict tool schemas, require the `/beta` base URL and are unstable; a basic smoke test should use the stable base URL.
- Handle provider errors explicitly: `401` authentication failure, `402` insufficient balance, `422` invalid parameters, `429` rate limit, and `500`/`503` transient server or overload errors.

Sources: [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode), [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/), [Error Codes](https://api-docs.deepseek.com/quick_start/error_codes/).

## Relay recommendation

For Relay's current API-backed OpenAI-compatible provider path, add DeepSeek as a provider override rather than changing the existing OpenAI defaults: base URL `https://api.deepseek.com`, model `deepseek-v4-flash`, and an environment-provided `DEEPSEEK_API_KEY`. For the first BYOK smoke test, send one non-streaming request with `thinking` explicitly disabled and assert HTTP success plus a non-empty `choices[0].message.content`; this keeps the test focused on transport/authentication and avoids reasoning-mode parameter behavior. This file makes no source or configuration changes.
