import {
  S3Client, PutObjectCommand, GetObjectCommand, DeleteObjectCommand, ListObjectsV2Command,
} from '@aws-sdk/client-s3';

const MAX_OBJECT = 8 * 1024 * 1024;
const PATH_PATTERN = /^[a-zA-Z0-9_./%-]{1,250}$/;

export function memoryStorageForTests() {
  const entries = new Map();
  return {
    async get(name) { return entries.has(name) ? Buffer.from(entries.get(name)) : null; },
    async put(name, bytes) { entries.set(name, Buffer.from(bytes)); },
    async del(name) { entries.delete(name); },
    async list(prefix) { return [...entries.keys()].filter((x) => x.startsWith(prefix)).sort(); },
  };
}

function checkKey(key) {
  if (typeof key !== 'string' || !PATH_PATTERN.test(key) ||
      key.startsWith('/') || key.includes('..') || key.includes('//')) {
    throw new Error('Invalid storage object key');
  }
  return key;
}

function checkStorageURL(raw) {
  const url = new URL(raw);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) {
    throw new Error('S3 HTTPS endpoint required');
  }
  const host = url.hostname.toLowerCase();
  if (host === 'localhost' || host.endsWith('.localhost') ||
      host.endsWith('.internal') || /^\d{1,3}(\.\d{1,3}){3}$/.test(host)) {
    throw new Error('S3 endpoint not permitted');
  }
  return raw;
}

export function createStorageFromEnv(env=process.env) {
  if (env.WA_STORAGE_BACKEND === 'MEMORY') {
    if (env.NODE_ENV !== 'test' || env.WA_ALLOW_MEMORY_FOR_TESTS !== 'true') {
      throw new Error('Ephemeral storage is disabled outside tests');
    }
    return memoryStorageForTests();
  }
  if (env.WA_STORAGE_BACKEND !== 'S3') throw new Error('Encrypted persistent S3 storage is required');
  const endpoint = checkStorageURL(env.WA_S3_ENDPOINT || '');
  const bucket = env.WA_S3_BUCKET || '';
  const keyId = env.WA_S3_ACCESS_KEY_ID || '';
  const secret = env.WA_S3_SECRET_ACCESS_KEY || '';
  if (!/^[a-z0-9][a-z0-9.-]{2,62}$/.test(bucket) || !keyId || !secret) {
    throw new Error('S3 credentials or bucket missing');
  }
  const client = new S3Client({
    endpoint, region: env.WA_S3_REGION || 'auto',
    forcePathStyle: env.WA_S3_URL_STYLE === 'path',
    maxAttempts: 2,
    credentials: { accessKeyId:keyId, secretAccessKey:secret },
  });
  return {
    async get(name) {
      const Key = checkKey(name);
      try {
        const response = await client.send(new GetObjectCommand({ Bucket:bucket, Key }));
        const size = Number(response.ContentLength || 0);
        if (size > MAX_OBJECT) throw new Error('Storage object too large');
        const buffer = Buffer.from(await response.Body.transformToByteArray());
        if (buffer.length > MAX_OBJECT) throw new Error('Storage object too large');
        return buffer;
      } catch (error) {
        if (error?.name === 'NoSuchKey' || error?.name === 'NotFound' ||
            error?.$metadata?.httpStatusCode === 404) return null;
        throw new Error('Storage read unavailable');
      }
    },
    async put(name, bytes) {
      checkKey(name);
      if (!Buffer.isBuffer(bytes) || bytes.length > MAX_OBJECT) throw new Error('Invalid storage content');
      try {
        await client.send(new PutObjectCommand({
          Bucket:bucket, Key:name, Body:bytes, ContentType:'application/octet-stream',
          CacheControl:'no-store',
        }));
      } catch { throw new Error('Storage write unavailable'); }
    },
    async del(name) {
      checkKey(name);
      try { await client.send(new DeleteObjectCommand({ Bucket:bucket, Key:name })); }
      catch { throw new Error('Storage delete unavailable'); }
    },
    async list(prefix) {
      checkKey(prefix);
      const result = [];
      let token;
      do {
        let response;
        try {
          response = await client.send(new ListObjectsV2Command({
            Bucket:bucket, Prefix:prefix, ContinuationToken:token, MaxKeys:1000,
          }));
        } catch { throw new Error('Storage list unavailable'); }
        for (const item of response.Contents || []) {
          if (typeof item.Key === 'string' && item.Key.startsWith(prefix)) result.push(item.Key);
          if (result.length > 10000) throw new Error('Storage key count exceeded');
        }
        token = response.IsTruncated ? response.NextContinuationToken : undefined;
      } while (token);
      return result;
    },
  };
}
