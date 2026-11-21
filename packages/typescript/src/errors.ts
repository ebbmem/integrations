import { redactPayload, redactSecrets } from "./security";

export class IntegrationCoreError extends Error {
  constructor(message = "") {
    super(message ? redactSecrets(message) : "");
    this.name = new.target.name;
    Object.setPrototypeOf(this, new.target.prototype);
  }

  override toString(): string {
    return `${this.name}: ${redactSecrets(this.message)}`;
  }
}

export class CredentialInUrlError extends IntegrationCoreError {}

export class CredentialConflictError extends IntegrationCoreError {}

export class ValidationError extends IntegrationCoreError {}

export class NamespaceError extends ValidationError {}

export class ReservedMetadataError extends ValidationError {}

export class UnsupportedCapabilityError extends IntegrationCoreError {}

export interface EbbmemApiErrorOptions {
  status?: number;
  code?: string;
  detail?: string;
  retryAfterMs?: number;
  body?: unknown;
}

export class EbbmemApiError extends IntegrationCoreError {
  readonly status?: number;
  readonly code?: string;
  readonly detail?: string;
  readonly retryAfterMs?: number;
  readonly body?: unknown;

  constructor(message: string, options: EbbmemApiErrorOptions = {}) {
    super(message);
    this.status = options.status;
    this.code = options.code;
    this.detail = options.detail ? redactSecrets(options.detail) : options.detail;
    this.retryAfterMs = options.retryAfterMs;
    this.body = options.body !== undefined ? redactPayload(options.body) : options.body;
  }
}

export class AuthError extends EbbmemApiError {}

export class NotFoundError extends EbbmemApiError {}

export class ApiValidationError extends EbbmemApiError {}

export class RateLimitError extends EbbmemApiError {}

export class IdempotencyConflictError extends EbbmemApiError {}

export class ServerError extends EbbmemApiError {}

export class TransportError extends EbbmemApiError {}

export class RequestTimeoutError extends TransportError {}
