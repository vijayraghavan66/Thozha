import { describe, expect, it } from 'vitest';
import { encodeCompactPairingQr } from './compactPairingQr';

describe('compact signed pairing QR', () => {
  const payload = {
    protocol: 'openjarvis-pairing-v2' as const,
    server_url: 'https://thozha-test.local:8443',
    gateway_address: '192.168.1.44',
    gateway_port: '8443' as const,
    gateway_certificate_sha256: `sha256:${'a'.repeat(64)}`,
    server_fingerprint: 'sha256:5cd252fb0ce8932436faf8ccd1040981b89ee4ad6b9fe9e2a2b7e71aacb27cd3',
    server_public_key: 'MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEaxfR8uEsQkf4vOblY6RA8ncDfYEt6zOg9KE5RdiYwpZP40Li/hp/m47n60p8D54WK84zV2sxXs7LtkBoN79R9Q==',
    invitation_id: '0123456789abcdef0123456789abcdef',
    challenge: 'cross-language-pairing-v2-test-challenge',
    expires_at: '2030-01-02T03:04:05+00:00',
    server_signature: 'MEUCIHjfiuOHqfRoB2W3tP6bVOGKiiQMs/y5gm4LLXM0lxwCAiEA1TqlCT1wZ04DDATTQ0naflN6DgVt0V3JFll7uVOYy/g=',
  };

  it('matches the Python serializer’s cross-language vector and shrinks the displayed code', () => {
    const compact = encodeCompactPairingQr(payload);
    expect(compact).toBe(
      'thozha-pair-v2:VFpQMgARdGhvemhhLXRlc3QubG9jYWzAqAEsIPuqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqlzSUvsM6JMkNvr4zNEECYG4nuSta5_p4qK35xqssnzTWzBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABGsX0fLhLEJH-Lzm5WOkQPJ3A32BLeszoPShOUXYmMKWT-NC4v4af5uO5-tKfA-eFivOM1drMV7Oy7ZAaDe_UfUQASNFZ4mrze8BI0VniavN7yhjcm9zcy1sYW5ndWFnZS1wYWlyaW5nLXYyLXRlc3QtY2hhbGxlbmdlGTIwMzAtMDEtMDJUMDM6MDQ6MDUrMDA6MDAARzBFAiB434rjh6n0aAdlt7T-m1ThiookDLP8uYJuCy1zNJccAgIhANU6pQk9cGdOAwwE00NJ2n5Teg4FbdFdyRZZe7lTmMv4'
    );
    expect(compact.length).toBeLessThan(JSON.stringify(payload).length * 0.65);
  });
});
