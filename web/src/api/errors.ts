export class WebApiError extends Error {
  constructor(
    readonly kind: 'http' | 'connection' | 'timeout' | 'protocol' | 'html' | 'integrity',
    readonly status?: number,
    readonly detail?: string
  ) {
    super(kind === 'http' ? `Browser API returned HTTP ${status}` : `Browser API ${kind} error`);
    this.name = 'WebApiError';
  }
}
