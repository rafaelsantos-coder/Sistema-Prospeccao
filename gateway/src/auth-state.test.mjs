import assert from 'node:assert/strict';
import { test } from 'node:test';
import { randomBytes } from 'node:crypto';
import { encryptedAuthState, persistedSessionIds } from './auth-state.mjs';
import { cryptoKey } from './security.mjs';
import { memoryStorageForTests } from './store.mjs';

test('credential state is encrypted and restored without mixing sessions', async () => {
  const store = memoryStorageForTests();
  const key = cryptoKey(randomBytes(32).toString('base64'));
  const first = await encryptedAuthState(store, 'sessionA', key);
  first.state.creds.registered = true;
  await first.saveCreds();
  const ciphertext = await store.get('sessions/sessionA/creds.bin');
  assert.ok(ciphertext instanceof Buffer);
  assert.equal(ciphertext.subarray(0,4).toString(), 'SW01');
  assert.equal(ciphertext.toString('utf8').includes('registered'), false);
  const restored = await encryptedAuthState(store, 'sessionA', key);
  assert.equal(restored.state.creds.registered, true);
  const other = await encryptedAuthState(store, 'sessionB', key);
  assert.notEqual(other.state.creds.registered, true);
  assert.deepEqual(await persistedSessionIds(store), ['sessionA']);
});

test('tampering or wrong decryption key refuses identity reuse', async () => {
  const store = memoryStorageForTests();
  const key = cryptoKey(randomBytes(32).toString('base64'));
  const first = await encryptedAuthState(store, 'sessionC', key);
  await first.saveCreds();
  const damaged = await store.get('sessions/sessionC/creds.bin');
  damaged[damaged.length - 1] ^= 1;
  await store.put('sessions/sessionC/creds.bin', damaged);
  await assert.rejects(() => encryptedAuthState(store, 'sessionC', key));
  const wrong = cryptoKey(randomBytes(32).toString('base64'));
  await assert.rejects(() => encryptedAuthState(store, 'sessionC', wrong));
});
