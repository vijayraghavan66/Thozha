export async function matchesSha256(data: BufferSource, expected: string): Promise<boolean> {
  if (!globalThis.crypto?.subtle) return false;
  const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', data));
  return Array.from(hash, byte => byte.toString(16).padStart(2, '0')).join('') === expected;
}
