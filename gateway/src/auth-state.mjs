import { initAuthCreds, BufferJSON, proto } from '@whiskeysockets/baileys';
import { encrypt, decrypt, validSessionId } from './security.mjs';

export async function encryptedAuthState(store, id, key) {
  if (!validSessionId(id)) throw new Error('Invalid session id');
  const prefix = 'sessions/' + id + '/';
  const read = async (name) => {
    const bytes = await store.get(prefix + name + '.bin');
    if (bytes === null) return null;
    try { return JSON.parse(decrypt(bytes, key).toString('utf8'), BufferJSON.reviver); }
    catch { throw new Error('Encrypted WhatsApp credentials cannot be decoded'); }
  };
  const write = async (name, value) => {
    const bytes = Buffer.from(JSON.stringify(value, BufferJSON.replacer));
    await store.put(prefix + name + '.bin', encrypt(bytes, key));
  };
  const remove = async (name) => store.del(prefix + name + '.bin');
  const creds = await read('creds') || initAuthCreds();
  return {
    state: {
      creds,
      keys: {
        get: async (type, ids) => {
          const data = {};
          for (const itemId of ids) {
            const stored = await read(type + '-' + encodeURIComponent(itemId));
            if (stored === null) continue;
            data[itemId] = type === 'app-state-sync-key'
              ? proto.Message.AppStateSyncKeyData.fromObject(stored) : stored;
          }
          return data;
        },
        set: async (records) => {
          for (const [type, entries] of Object.entries(records)) {
            for (const [itemId, value] of Object.entries(entries)) {
              const name = type + '-' + encodeURIComponent(itemId);
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

export async function persistedSessionIds(store) {
  const paths = await store.list('sessions/');
  return [...new Set(paths.map((p) => p.split('/')[1]))].filter(validSessionId).sort();
}
