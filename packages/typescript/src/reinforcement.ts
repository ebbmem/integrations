import { ValidationError } from "./errors";

export type ReinforceMode = "tool" | "cited" | "feedback" | "injected";

export const DEFAULT_REINFORCE_DELTA = 0.1;
export const DEFAULT_FRAMEWORK_REINFORCE_MODE: ReinforceMode = "cited";
export const DEFAULT_MCP_REINFORCE_MODE: ReinforceMode = "tool";
export const DEFAULT_EXPOSURE_CAPACITY = 512;

const VALID_MODES: ReadonlySet<string> = new Set(["tool", "cited", "feedback", "injected"]);

export function validateDelta(delta: unknown, options: { clamp?: boolean } = {}): number {
  if (typeof delta === "boolean" || typeof delta !== "number" || !Number.isFinite(delta)) {
    throw new ValidationError("invalid_delta: delta must be a finite number");
  }
  if (options.clamp) {
    return Math.max(-1.0, Math.min(1.0, delta));
  }
  if (delta < -1.0 || delta > 1.0) {
    throw new ValidationError("delta_out_of_bounds: delta must be within [-1.0, 1.0]");
  }
  return delta;
}

function escapeRegExp(literal: string): string {
  return literal.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function extractCitedIds(text: string, candidateIds: readonly string[]): string[] {
  if (!text || candidateIds.length === 0) {
    return [];
  }
  const found: Array<{ index: number; id: string }> = [];
  const seen = new Set<string>();
  for (const memId of candidateIds) {
    if (!memId || seen.has(memId)) continue;
    const re = new RegExp(`(?<![A-Za-z0-9_-])${escapeRegExp(memId)}(?![A-Za-z0-9_-])`);
    const match = re.exec(text);
    if (match !== null) {
      seen.add(memId);
      found.push({ index: match.index, id: memId });
    }
  }
  found.sort((a, b) => a.index - b.index);
  return found.map((item) => item.id);
}

export interface ReinforcePolicyOptions {
  mode?: ReinforceMode;
  injected?: boolean;
}

export class ReinforcePolicy {
  readonly mode: ReinforceMode;
  readonly injected: boolean;

  constructor(options: ReinforcePolicyOptions = {}) {
    const mode = options.mode ?? DEFAULT_FRAMEWORK_REINFORCE_MODE;
    if (!VALID_MODES.has(mode)) {
      throw new ValidationError(`Unsupported reinforcement mode: ${String(mode)}`);
    }
    this.mode = mode;
    this.injected = options.injected ?? false;
  }

  selectIdsToReinforce(
    recalledIds: readonly string[],
    usedIds: readonly string[],
    injected?: boolean,
  ): string[] {
    const useInjected =
      injected !== undefined ? injected : this.injected || this.mode === "injected";
    if (useInjected) {
      const seen = new Set<string>();
      const ordered: string[] = [];
      for (const itemId of [...usedIds, ...recalledIds]) {
        if (itemId && !seen.has(itemId)) {
          seen.add(itemId);
          ordered.push(itemId);
        }
      }
      return ordered;
    }

    const recalledSet = new Set(recalledIds.filter(Boolean));
    const seenUsed = new Set<string>();
    const result: string[] = [];
    for (const itemId of usedIds) {
      if (itemId && recalledSet.has(itemId) && !seenUsed.has(itemId)) {
        seenUsed.add(itemId);
        result.push(itemId);
      }
    }
    return result;
  }
}

export interface ExposureRecord {
  namespace: string;
  turnId?: string;
  recallId?: string;
  recalledIds: string[];
  ranks: Record<string, number>;
  scores: Record<string, number>;
  timestampMs: number;
}

export interface RecallResponseLike {
  recall_id?: string | null;
  results?: Array<{
    id?: string;
    memory_id?: string;
    score?: number;
    [key: string]: unknown;
  }>;
}

export class ExposureBookkeeper {
  readonly maxEntries: number;
  private readonly records = new Map<number, ExposureRecord>();
  private readonly byTurn = new Map<string, number>();
  private readonly byRecallId = new Map<string, number>();
  private readonly latestByNs = new Map<string, number>();
  private seq = 0;

  constructor(maxEntries = DEFAULT_EXPOSURE_CAPACITY) {
    if (!Number.isInteger(maxEntries) || maxEntries < 1) {
      throw new ValidationError("maxEntries must be an integer >= 1");
    }
    this.maxEntries = maxEntries;
  }

  private static turnKey(namespace: string, turnId: string): string {
    return `${namespace}\u0000${turnId}`;
  }

  private static recallKey(namespace: string, recallId: string): string {
    return `${namespace}\u0000${recallId}`;
  }

  private touchSeq(seqId: number): ExposureRecord | undefined {
    const rec = this.records.get(seqId);
    if (!rec) return undefined;
    this.records.delete(seqId);
    this.records.set(seqId, rec);
    return rec;
  }

  private evictSeq(seqId: number): void {
    const rec = this.records.get(seqId);
    if (!rec) return;
    this.records.delete(seqId);
    if (rec.turnId !== undefined) {
      const tKey = ExposureBookkeeper.turnKey(rec.namespace, rec.turnId);
      if (this.byTurn.get(tKey) === seqId) {
        this.byTurn.delete(tKey);
      }
    }
    if (rec.recallId !== undefined) {
      const rKey = ExposureBookkeeper.recallKey(rec.namespace, rec.recallId);
      if (this.byRecallId.get(rKey) === seqId) {
        this.byRecallId.delete(rKey);
      }
    }
    if (this.latestByNs.get(rec.namespace) === seqId) {
      this.latestByNs.delete(rec.namespace);
    }
  }

  recordRecall(
    namespace: string,
    recallResponse: RecallResponseLike,
    options: { turnId?: string; timestampMs?: number } = {},
  ): ExposureRecord {
    const rawResults = Array.isArray(recallResponse.results) ? recallResponse.results : [];
    const recalledIds: string[] = [];
    const ranks: Record<string, number> = {};
    const scores: Record<string, number> = {};

    rawResults.forEach((item, idx) => {
      if (!item || typeof item !== "object") return;
      const memId = item.memory_id || item.id;
      if (typeof memId !== "string" || !memId) return;
      if (!(memId in ranks)) {
        recalledIds.push(memId);
        ranks[memId] = idx + 1;
        if (typeof item.score === "number" && Number.isFinite(item.score)) {
          scores[memId] = item.score;
        }
      }
    });

    const rawRecallId = recallResponse.recall_id;
    const recallId =
      typeof rawRecallId === "string" && rawRecallId.trim() ? rawRecallId.trim() : undefined;

    const record: ExposureRecord = {
      namespace,
      turnId: options.turnId,
      recallId,
      recalledIds,
      ranks,
      scores,
      timestampMs: options.timestampMs ?? Date.now(),
    };

    this.seq += 1;
    const seqId = this.seq;
    this.records.set(seqId, record);
    this.latestByNs.set(namespace, seqId);
    if (options.turnId !== undefined) {
      this.byTurn.set(ExposureBookkeeper.turnKey(namespace, options.turnId), seqId);
    }
    if (recallId !== undefined) {
      this.byRecallId.set(ExposureBookkeeper.recallKey(namespace, recallId), seqId);
    }

    while (this.records.size > this.maxEntries) {
      const oldest = this.records.keys().next().value;
      if (oldest === undefined) break;
      this.evictSeq(oldest);
    }

    return record;
  }

  getLatest(namespace: string): ExposureRecord | undefined {
    const seqId = this.latestByNs.get(namespace);
    return seqId !== undefined ? this.touchSeq(seqId) : undefined;
  }

  getForTurn(namespace: string, turnId: string): ExposureRecord | undefined {
    const seqId = this.byTurn.get(ExposureBookkeeper.turnKey(namespace, turnId));
    return seqId !== undefined ? this.touchSeq(seqId) : undefined;
  }

  getByRecallId(recallId: string, namespace?: string): ExposureRecord | undefined {
    if (namespace !== undefined) {
      const seqId = this.byRecallId.get(ExposureBookkeeper.recallKey(namespace, recallId));
      return seqId !== undefined ? this.touchSeq(seqId) : undefined;
    }
    for (const [seqId, rec] of [...this.records.entries()].reverse()) {
      if (rec.recallId === recallId) {
        return this.touchSeq(seqId);
      }
    }
    return undefined;
  }

  consumeTurn(namespace: string, turnId: string): ExposureRecord | undefined {
    const seqId = this.byTurn.get(ExposureBookkeeper.turnKey(namespace, turnId));
    if (seqId === undefined) return undefined;
    const rec = this.records.get(seqId);
    this.evictSeq(seqId);
    return rec;
  }

  clear(namespace?: string): void {
    if (namespace === undefined) {
      this.records.clear();
      this.byTurn.clear();
      this.byRecallId.clear();
      this.latestByNs.clear();
      return;
    }
    for (const [seqId, rec] of [...this.records.entries()]) {
      if (rec.namespace === namespace) {
        this.evictSeq(seqId);
      }
    }
  }
}

export interface ReinforcePlan {
  memoryId: string;
  usedIds: string[];
  recallId?: string;
  delta: number;
  namespace: string;
}

export class ReinforcementTracker {
  readonly policy: ReinforcePolicy;
  readonly bookkeeper: ExposureBookkeeper;

  constructor(policy?: ReinforcePolicy, bookkeeper?: ExposureBookkeeper) {
    this.policy = policy ?? new ReinforcePolicy();
    this.bookkeeper = bookkeeper ?? new ExposureBookkeeper();
  }

  recordExposure(
    namespace: string,
    recallResponse: RecallResponseLike,
    options: { turnId?: string; timestampMs?: number } = {},
  ): ExposureRecord {
    return this.bookkeeper.recordRecall(namespace, recallResponse, options);
  }

  planReinforcement(
    namespace: string,
    options: {
      usedIds?: readonly string[];
      responseText?: string;
      turnId?: string;
      recallId?: string;
      delta?: number;
      clampDelta?: boolean;
      mode?: ReinforceMode;
      injected?: boolean;
    } = {},
  ): ReinforcePlan | undefined {
    const validatedDelta = validateDelta(options.delta ?? DEFAULT_REINFORCE_DELTA, {
      clamp: options.clampDelta,
    });
    const effectiveMode = options.mode ?? this.policy.mode;
    if (!VALID_MODES.has(effectiveMode)) {
      throw new ValidationError(`Unsupported reinforcement mode: ${String(effectiveMode)}`);
    }

    const explicitRecallId =
      typeof options.recallId === "string" && options.recallId.trim()
        ? options.recallId.trim()
        : undefined;
    let exposure: ExposureRecord | undefined;
    if (explicitRecallId !== undefined) {
      exposure = this.bookkeeper.getByRecallId(explicitRecallId, namespace);
    } else if (options.turnId !== undefined) {
      exposure = this.bookkeeper.getForTurn(namespace, options.turnId);
    } else {
      exposure = this.bookkeeper.getLatest(namespace);
    }

    const recalledIds = exposure?.recalledIds ?? [];
    let resolvedRecallId = explicitRecallId ?? exposure?.recallId;

    let candidateUsed = [...(options.usedIds ?? [])];
    if (
      candidateUsed.length === 0 &&
      effectiveMode === "cited" &&
      options.responseText &&
      recalledIds.length > 0
    ) {
      candidateUsed = extractCitedIds(options.responseText, recalledIds);
    }

    let selected: string[];
    if (effectiveMode === "feedback" && candidateUsed.length > 0) {
      const seenFb = new Set<string>();
      selected = [];
      for (const uid of candidateUsed) {
        if (uid && !seenFb.has(uid)) {
          seenFb.add(uid);
          selected.push(uid);
        }
      }
      if (explicitRecallId === undefined && options.turnId === undefined) {
        resolvedRecallId = undefined;
      }
    } else {
      const effectiveInjected =
        options.injected !== undefined
          ? options.injected
          : effectiveMode === "injected" || this.policy.injected;
      selected = this.policy.selectIdsToReinforce(recalledIds, candidateUsed, effectiveInjected);
    }

    if (selected.length === 0) {
      return undefined;
    }

    return {
      memoryId: selected[0],
      usedIds: selected.slice(1),
      recallId: resolvedRecallId,
      delta: validatedDelta,
      namespace,
    };
  }
}
