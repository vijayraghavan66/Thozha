// Isolated, public-only QR payload factory. Never imported by production UI.
export function expiringInvitation(invitationId: string, expiresAt: string) {
  const fingerprint = `sha256:${'e'.repeat(64)}`;
  const payload = {
    protocol: 'openjarvis-pairing-v2',
    server_url: 'https://192.0.2.1:8443',
    gateway_address: '192.0.2.1',
    gateway_port: '8443',
    gateway_certificate_sha256: fingerprint,
    server_fingerprint: fingerprint,
    server_public_key: 'fixture-public-only',
    invitation_id: invitationId,
    challenge: 'fixture-only-challenge',
    expires_at: expiresAt,
    server_signature: 'fixture-only-signature',
  };
  return { ...payload, qr_payload: payload };
}
