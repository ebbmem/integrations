import {
  EbbmemTransport,
  MemoryScopedParams,
  RecallParams,
  ReinforceParams,
  RememberParams,
  SdkCapabilities,
} from "ebbmem-integration-core";

export interface RecordedRememberCall extends RememberParams {}
export interface RecordedRecallCall extends RecallParams {}
export interface RecordedReinforceCall extends ReinforceParams {}
export interface RecordedScopedCall {
  memoryId: string;
  params: MemoryScopedParams;
}

export class MockEbbmemTransport implements EbbmemTransport {
  readonly capabilities: SdkCapabilities = {
    tierOnRemember: true,
    sessionIdOnRemember: true,
    metadataOnRemember: true,
    recallFilters: true,
    recallBreakdownAndId: true,
    reinforceUsedIdsAndRecallId: true,
    reinforceDefaultDelta01: true,
    deterministicIdempotencyKey: true,
    exactKeyGet: false,
    exactKeyDelete: false,
    metadataFilterOnListRecall: false,
    namespacePrefixList: false,
    notes: ["In-memory mock transport for @ebbmem/ai-sdk unit tests"],
  };

  readonly rememberCalls: RecordedRememberCall[] = [];
  readonly recallCalls: RecordedRecallCall[] = [];
  readonly reinforceCalls: RecordedReinforceCall[] = [];
  readonly getCalls: RecordedScopedCall[] = [];
  readonly forgetCalls: RecordedScopedCall[] = [];
  readonly purgeCalls: RecordedScopedCall[] = [];

  recallResponse: Record<string, unknown> = {
    query: "",
    count: 0,
    results: [],
  };

  failRecallWith?: Error | (() => Error);
  failRememberWith?: Error | (() => Error);
  failReinforceWith?: Error | (() => Error);
  private nextId = 1;

  async remember(params: RememberParams): Promise<Record<string, unknown>> {
    this.rememberCalls.push(params);
    if (this.failRememberWith) {
      throw typeof this.failRememberWith === "function"
        ? this.failRememberWith()
        : this.failRememberWith;
    }
    const id = `mem-${this.nextId++}`;
    return {
      id,
      memory_id: id,
      namespace: params.namespace,
      tier: params.tier ?? "semantic",
      content: params.content,
      tags: params.tags ?? [],
    };
  }

  async recall(params: RecallParams): Promise<Record<string, unknown>> {
    this.recallCalls.push(params);
    if (this.failRecallWith) {
      throw typeof this.failRecallWith === "function"
        ? this.failRecallWith()
        : this.failRecallWith;
    }
    return {
      ...this.recallResponse,
      query: params.query,
    };
  }

  async reinforce(params: ReinforceParams): Promise<Record<string, unknown>> {
    this.reinforceCalls.push(params);
    if (this.failReinforceWith) {
      throw typeof this.failReinforceWith === "function"
        ? this.failReinforceWith()
        : this.failReinforceWith;
    }
    return {
      ok: true,
      memory_id: params.memoryId,
      used_ids: params.usedIds ?? [],
      recall_id: params.recallId,
      delta: params.delta ?? 0.1,
    };
  }

  async get(
    memoryId: string,
    params: MemoryScopedParams = {},
  ): Promise<Record<string, unknown>> {
    this.getCalls.push({ memoryId, params });
    return { id: memoryId, memory_id: memoryId };
  }

  async forget(
    memoryId: string,
    params: MemoryScopedParams = {},
  ): Promise<Record<string, unknown>> {
    this.forgetCalls.push({ memoryId, params });
    return { id: memoryId, memory_id: memoryId, tombstoned: true };
  }

  async purge(
    memoryId: string,
    params: MemoryScopedParams = {},
  ): Promise<Record<string, unknown>> {
    this.purgeCalls.push({ memoryId, params });
    return { id: memoryId, memory_id: memoryId, purged: true };
  }

  async status(): Promise<Record<string, unknown>> {
    return { ok: true };
  }

  async health(): Promise<Record<string, unknown>> {
    return { ok: true };
  }
}
