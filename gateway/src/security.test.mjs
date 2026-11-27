import assert from 'node:assert/strict';
import { test } from 'node:test';
import { randomBytes } from 'node:crypto';
import { equalSecret, cryptoKey, encrypt, decrypt, signBody, validSessionId, validPhone } from './security.mjs';

test('encryption is authenticated and rejects tampering', () => {
  const key = cryptoKey(randomBytes(32).toString('base64'));
  const data = Buffer.from('sensitive session secrets');
  const ciphertext = encrypt(data, key);
  assert.notEqual(ciphertext.toString(), data.toString());
  assert.deepEqual(decrypt(ciphertext, key), data);
  ciphertext[ciphertext.length - 1] ^= 1;
  assert.throws(() => decrypt(ciphertext, key));
});
test('reject weak key, invalid ids and phones', () => {
  assert.throws(() => cryptoKey('abc'));
  assert.equal(validSessionId('../escape'), false);
  assert.equal(validSessionId('test-session-01'), true);
  assert.equal(validPhone('5511999999999'), true);
  assert.equal(validPhone('5511999999999@s.whatsapp.net'), false);
});
test('constant time credential equality and webhook integrity', () => {
  assert.equal(equalSecret('123456789', '123456789'), true);
  assert.equal(equalSecret('123456789', '123456780'), false);
  assert.equal(equalSecret('abc', 'abcdef'), false);
  assert.notEqual(signBody('hello', 123, 'key'), signBody('hell0', 123, 'key'));
});
