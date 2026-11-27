import { createCipheriv, createDecipheriv, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';

export function equalSecret(actual, expected) {
  if (typeof actual !== 'string' || typeof expected !== 'string') return false;
  const a = Buffer.from(actual);
  const b = Buffer.from(expected);
  return a.length > 0 && b.length > 0 && a.length === b.length && timingSafeEqual(a, b);
}

export function cryptoKey(value) {
  const key = Buffer.from(value || '', 'base64');
  if (key.length !== 32 || key.toString('base64') !== value) throw new Error('WA_ENCRYPTION_KEY invalid');
  return key;
}

export function encrypt(data, key) {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, iv);
  const body = Buffer.concat([cipher.update(data), cipher.final()]);
  return Buffer.concat([Buffer.from('SW01'), iv, cipher.getAuthTag(), body]);
}

export function decrypt(blob, key) {
  if (!Buffer.isBuffer(blob) || blob.length < 32 || blob.subarray(0, 4).toString() !== 'SW01') {
    throw new Error('Encrypted content invalid');
  }
  const decipher = createDecipheriv('aes-256-gcm', key, blob.subarray(4, 16));
  decipher.setAuthTag(blob.subarray(16, 32));
  return Buffer.concat([decipher.update(blob.subarray(32)), decipher.final()]);
}

export function signBody(body, timestamp, secret) {
  return createHmac('sha256', secret).update(String(timestamp)).update('.').update(body).digest('hex');
}

export function validSessionId(id) {
  return typeof id === 'string' && /^[a-zA-Z0-9-]{3,80}$/.test(id);
}

export function validPhone(phone) {
  return typeof phone === 'string' && /^[1-9][0-9]{9,14}$/.test(phone);
}
