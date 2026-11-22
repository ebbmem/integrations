# `@ebbmem/ai-sdk`

Vercel AI SDK (`ai` v7+) tools and `wrapLanguageModel` middleware for [EbbMem](https://ebbmem.ai) cognitive memory, built on `ebbmem-integration-core`.

## Why Cognitive Memory Matters Here

1. **Read-only recall (`reinforce: false`)**: Recalling memories never inflates storage strength or resets spacing intervals.
2. **Explicit usage reinforcement (`injected: false` default)**: Only memories that are actually cited or explicitly reinforced get boosted (`delta` in `[-1.0, 1.0]`, default `+0.1`), preventing popularity loops.
3. **Untrusted context framing**: Recalled memories are wrapped in `<ebbmem_untrusted_memory_context>` delimiters with attribute and closing-tag escaping so recalled data cannot override system instructions.
4. **Fail-open resilience & header-only auth**: Memory outages degrade gracefully without breaking model generation, secrets are redacted before storage/logging, and credentials travel strictly via `Authorization: Bearer` or `X-API-Key` headers.

## Installation (Local Monorepo Development)

Build the shared TypeScript core first, then install and build `@ebbmem/ai-sdk`:

```bash
cd packages/typescript
npm install --no-package-lock
npm run build

cd ../ai-sdk
npm install --no-package-lock
npm run typecheck
npm run build
npm test
```

## Exports

- `ebbmemTools(options?)` — returns `{ memory_recall, memory_remember, memory_reinforce, memory_forget }` using Vercel AI SDK `tool()` + `zod` schemas.
- `withEbbmem(model, options?)` — wraps a `LanguageModelV4` / `LanguageModelV3` / `LanguageModelV2` with `ebbmemMiddleware(options)` via `wrapLanguageModel`.
- `ebbmemMiddleware(options?)` — standalone `LanguageModelMiddleware` for composition with other AI SDK middlewares.

## Example: Next.js App Router Route (Two Providers)

> **Note:** The live Next.js route snippet below has **not been live-tested** against external LLM provider APIs (`@ai-sdk/openai`, `@ai-sdk/anthropic`) or a hosted EbbMem deployment; automated verification covers `MockLanguageModelV4` / `MockLanguageModelV3` and the local IC-1 mock server.

```ts
// app/api/chat/route.ts
import { anthropic } from "@ai-sdk/anthropic";
import { openai } from "@ai-sdk/openai";
import { convertToModelMessages, stepCountIs, streamText } from "ai";
import { ebbmemTools, withEbbmem } from "@ebbmem/ai-sdk";
import { EbbmemIntegrationCore, ScopingResolver } from "ebbmem-integration-core";

function selectBaseModel(provider: string | undefined) {
  switch (provider) {
    case "anthropic":
      return anthropic("claude-sonnet-4-5");
    case "openai":
    default:
      return openai("gpt-4o");
  }
}

export async function POST(req: Request) {
  const { messages, userId, sessionId, provider } = await req.json();

  // Share one core per request so middleware and tools share exposure tracking.
  const core = new EbbmemIntegrationCore({
    baseUrl: process.env.EBBMEM_BASE_URL ?? "http://127.0.0.1:8321",
    apiKey: process.env.EBBMEM_API_KEY,
    scope: new ScopingResolver({
      tenant: process.env.EBBMEM_TENANT ?? "default",
      userId: String(userId || "anonymous"),
      defaultSessionId: sessionId ? String(sessionId) : undefined,
    }),
  });

  const model = withEbbmem(selectBaseModel(provider), {
    core,
    rememberTier: "episodic",
    failOpen: true,
  });

  const result = streamText({
    model,
    system:
      "You are a helpful assistant. When you rely on a memory from <ebbmem_untrusted_memory_context>, cite its memory id (for example, [mem_...]) or call memory_reinforce.",
    messages: await convertToModelMessages(messages),
    tools: ebbmemTools({ core }),
    stopWhen: stepCountIs(5),
  });

  return result.toUIMessageStreamResponse();
}
```

## License

Licensed under the [Apache License, Version 2.0](../../LICENSE).
