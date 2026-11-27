import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createStorageFromEnv, memoryStorageForTests } from './store.mjs';

test('memory backend is explicitly test-only and never allowed in prod', () => {
  assert.throws(() => createStorageFromEnv({
    WA_STORAGE_BACKEND:'MEMORY', NODE_ENV:'production',
    WA_ALLOW_MEMORY_FOR_TESTS:'true',
  }));
  assert.throws(() => createStorageFromEnv({
    WA_STORAGE_BACKEND:'MEMORY', NODE_ENV:'test',
    WA_ALLOW_MEMORY_FOR_TESTS:'false',
  }));
  assert.throws(() => createStorageFromEnv({ WA_STORAGE_BACKEND:'FILES' }));
});

test('encrypted object store API supports put/get/list/del', async () => {
  const s = memoryStorageForTests();
  await s.put('sessions/test01/creds.bin', Buffer.from('encrypted'));
  assert.deepEqual(await s.get('sessions/test01/creds.bin'), Buffer.from('encrypted'));
  assert.deepEqual(await s.list('sessions/'), ['sessions/test01/creds.bin']);
  await s.del('sessions/test01/creds.bin');
  assert.equal(await s.get('sessions/test01/creds.bin'), null);
});

test('S3 backend validates HTTPS, credentials and host', () => {
  const ok={WA_STORAGE_BACKEND:'S3',WA_S3_ENDPOINT:'https://s3.example.com',
    WA_S3_BUCKET:'sulnet-wa',WA_S3_ACCESS_KEY_ID:'ak',
    WA_S3_SECRET_ACCESS_KEY:'secret'};
  assert.ok(createStorageFromEnv(ok));
  for(const endpoint of ['http://s3.example.com','https://localhost',
                         'https://127.0.0.1','https://evil.test?x=1']) {
    assert.throws(() => createStorageFromEnv({ ...ok, WA_S3_ENDPOINT:endpoint }));
  }
});
