import type { components } from '../api/generated';

type PairingPayload = components['schemas']['PairingQrPayloadV2'];
const PREFIX = 'thozha-pair-v2:';
const encoder = new TextEncoder();

function fromHex(value: string, bytes: number): Uint8Array {
  const normalized = value.startsWith('sha256:') ? value.slice(7) : value;
  if (!new RegExp(`^[0-9a-f]{${bytes * 2}}$`).test(normalized)) {
    throw new Error('Pairing invitation has a malformed signed fingerprint.');
  }
  return Uint8Array.from({ length: bytes }, (_, index) =>
    Number.parseInt(normalized.slice(index * 2, index * 2 + 2), 16)
  );
}

function fromBase64(value: string): Uint8Array {
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function withLength(value: Uint8Array): Uint8Array {
  if (value.length > 255) throw new Error('Pairing invitation field is too large.');
  return Uint8Array.of(value.length, ...value);
}

/** Compact transport serialization only; all original signed fields are preserved for Android verification. */
export function encodeCompactPairingQr(payload: PairingPayload): string {
  const server = new URL(payload.server_url);
  const port = Number(payload.gateway_port);
  if (
    server.protocol !== 'https:' ||
    !server.hostname.endsWith('.local') ||
    server.port !== String(port) ||
    (port !== 8443 && port !== 8444)
  ) {
    throw new Error('Pairing invitation does not target a supported private gateway.');
  }
  const address = payload.gateway_address.split('.').map(Number);
  if (
    address.length !== 4 ||
    address.some((part) => !Number.isInteger(part) || part < 0 || part > 255) ||
    address.join('.') !== payload.gateway_address
  ) {
    throw new Error('Pairing invitation has an invalid private address.');
  }
  const host = encoder.encode(server.hostname.toLowerCase());
  const challengeHex = /^[0-9a-f]{64}$/.test(payload.challenge);
  const challenge = challengeHex
    ? fromHex(payload.challenge, 32)
    : encoder.encode(payload.challenge);
  const gatewayAddress = Uint8Array.from(address);
  const portBytes = Uint8Array.of((port >>> 8) & 0xff, port & 0xff);
  const body = [
    encoder.encode('TZP2'),
    Uint8Array.of(challengeHex ? 1 : 0),
    withLength(host),
    gatewayAddress,
    portBytes,
    fromHex(payload.gateway_certificate_sha256, 32),
    fromHex(payload.server_fingerprint, 32),
    withLength(fromBase64(payload.server_public_key)),
    withLength(fromHex(payload.invitation_id, 16)),
    withLength(challenge),
    withLength(encoder.encode(payload.expires_at)),
    withLength(payload.loopback_url ? encoder.encode(payload.loopback_url) : new Uint8Array()),
    withLength(fromBase64(payload.server_signature)),
  ];
  const packed = new Uint8Array(body.reduce((total, part) => total + part.length, 0));
  let offset = 0;
  for (const part of body) {
    packed.set(part, offset);
    offset += part.length;
  }
  let binary = '';
  for (const byte of packed) binary += String.fromCharCode(byte);
  const encoded = btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return PREFIX + encoded;
}
