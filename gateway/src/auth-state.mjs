import fs from 'node:fs/promises';
import path from 'node:path';
import { initAuthCreds, BufferJSON, proto } from '@whiskeysockets/baileys';
import { encrypt, decrypt, validSessionId } from './security.mjs';

async function safeWrite(file, bytes) {
  const dir = path.dirname(file);
  await fs.mkdir(dir, { recursive: true, mode: 0o700 });
  const tmp = file + '.' + process.pid + '.' + Date.now() + '.tmp';
  try {
    await fs.writeFile(tmp, bytes, { mode: 0o600, flag: 'wx' });
    await fs.rename(tmp, file);
  } finally {
    await fs.rm(tmp, { force: true }).catch(() => {});
  }
}

export async function encryptedAuthState(root, id, key) {
  if (!validSessionId(id)) throw new Error('Invalid session id');
  const folder = path.join(root, 'sessions', id);
  await fs.mkdir(folder, { recursive: true, mode: 0o700 });

  const read = async (name) => {
    const file = path.join(folder, name + '.bin');
    try {
      const bytes = await fs.readFile(file);
      return JSON.parse(decrypt(bytes, key).toString('utf8'), BufferJSON.reviver);
    } catch (error) {
      if (error.code === 'ENOENT') return null;
      // Never silently generate a fresh WhatsApp identity if stored credentials are unreadable.
      throw new Error('Encrypted WhatsApp session state cannot be read');
    }
  };
  const write = async (name, value) => {
    const bytes = Buffer.from(JSON.stringify(value, BufferJSON.replacer));
    await safeWrite(path.join(folder, name + '.bin'), encrypt(bytes, key));
  };
  const remove = async (name) => fs.rm(path.join(folder, name + '.bin'), { force: true });
  const creds = await read('creds') || initAuthCreds();
  return {
    state: {
      creds,
      keys: {
        get: async (type, ids) => {
          const data = {};
          for (const id of ids) {
            const stored = await read(type + '-' + encodeURIComponent(id));
            if (stored === null) continue;
            data[id] = type === 'app-state-sync-key'
              ? proto.Message.AppStateSyncKeyData.fromObject(stored) : stored;
          }
          return data;
        },
        set: async (records) => {
          for (const [type, entries] of Object.entries(records)) {
            for (const [id, value] of Object.entries(entries)) {
              const name = type + '-' + encodeURIComponent(id);
              if (value == null) await remove(name);
              else await write(name, value);
            }
          }
        },
      },
    },
    saveCreds: () => write('creds', creds),
  };
}

export async function persistedSessionIds(root) {
  const directory = path.join(root, 'sessions');
  const entries = await fs.readdir(directory, { withFileTypes: true }).catch((error) => {
    if (error.code === 'ENOENT') return [];
    throw error;
  });
  return entries.filter((entry) => entry.isDirectory() && validSessionId(entry.name))
    .map((entry) => entry.name);
}
