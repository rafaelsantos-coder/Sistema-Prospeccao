import http from 'node:http';
import { randomUUID, createHash } from 'node:crypto';
import makeWASocket, { DisconnectReason, getContentType } from '@whiskeysockets/baileys';
import pino from 'pino';
import QRCode from 'qrcode';
import { encryptedAuthState, persistedSessionIds } from './auth-state.mjs';
import { createStorageFromEnv } from './store.mjs';
import { equalSecret, cryptoKey, signBody, validPhone, validSessionId, encrypt, decrypt } from './security.mjs';

const store = createStorageFromEnv();
const sharedKey = process.env.WA_API_KEY || '';
const hookKey = process.env.WA_WEBHOOK_KEY || '';
const hookURL = process.env.WA_WEBHOOK_URL || '';
const key = cryptoKey(process.env.WA_ENCRYPTION_KEY || '');
const port = Number(process.env.PORT || 3000);
if (sharedKey.length < 40 || hookKey.length < 40 || !hookURL.startsWith('https://')) {
  throw new Error('WA_API_KEY, WA_WEBHOOK_KEY or WA_WEBHOOK_URL not safely configured');
}
if (!Number.isInteger(port) || port <= 0 || port > 65535) throw new Error('PORT invalid');
const gatewaySessions = new Map();
const logger = pino({ level: 'silent' });
const MAX_BODY = 8192;
const queuePrefix = 'outbox/';
let flushingOutbox = false;

async function persistEvent(event) {
  const objectKey = queuePrefix + event.eventId + '.bin';
  if (await store.get(objectKey) !== null) return;
  await store.put(objectKey, encrypt(Buffer.from(JSON.stringify(event)), key));
}
async function sendEvent(objectKey) {
  const blob = await store.get(objectKey);
  if (blob === null) return;
  const body = decrypt(blob, key).toString('utf8');
  const timestamp = Math.floor(Date.now() / 1000);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(hookURL, {
      method:'POST',
      headers:{
        'Content-Type':'application/json',
        'X-Sulnet-WA-Timestamp':String(timestamp),
        'X-Sulnet-WA-Signature':signBody(body,timestamp,hookKey),
      },
      body, signal:controller.signal, redirect:'error',
    });
    if (response.ok) await store.del(objectKey);
  } catch {
    // Keep encrypted event for retry; never log personal data.
  } finally {
    clearTimeout(timer);
  }
}
async function flushOutbox() {
  if (flushingOutbox) return;
  flushingOutbox = true;
  try {
    const files = await store.list(queuePrefix);
    for (const name of files.filter((n) => /^outbox\/[a-f0-9]{64}\.bin$/.test(n)).slice(0, 100)) {
      try { await sendEvent(name); }
      catch { /* Keep for future retries. */ }
    }
  } finally {
    flushingOutbox = false;
  }
}
async function queueEvent(sessionId, messageId, from, text, kind) {
  const eventId = createHash('sha256').update(sessionId + '\0' + messageId).digest('hex');
  await persistEvent({
    eventId, sessionId, messageId, from, text, kind,
    timestamp: Math.floor(Date.now() / 1000), source: 'SULNET_WA',
  });
  void flushOutbox();
}
async function createSession(id) {
  if (!validSessionId(id)) throw new Error('Invalid session id');
  if (gatewaySessions.has(id)) return gatewaySessions.get(id);
  const record = {
    id, status: 'created', socket: null, qr: '', reconnect: null, stopped: true,
    lastError: null, phone: null,
  };
  await store.put('sessions/' + id + '/created.bin', encrypt(Buffer.from('created'), key));
  gatewaySessions.set(id, record);
  return record;
}
async function startSession(id) {
  const record = await createSession(id);
  if (!record.stopped && record.socket) return record;
  record.stopped = false;
  record.qr = '';
  record.status = 'initializing';
  const auth = await encryptedAuthState(store, id, key);
  try {
    const socket = makeWASocket({
      auth: auth.state, logger,
      printQRInTerminal: false,
      markOnlineOnConnect: false,
      syncFullHistory: false,
      generateHighQualityLinkPreview: false,
      getMessage: async () => undefined,
    });
    record.socket = socket;
    socket.ev.on('creds.update', async () => {
      try { await auth.saveCreds(); }
      catch {
        record.stopped = true;
        record.socket = null;
        record.status = 'failed';
        record.lastError = 'credentials-write';
        try { socket.end(undefined); } catch { /* fail closed */ }
      }
    });
    socket.ev.on('connection.update', ({ connection, qr, lastDisconnect }) => {
      if (record.stopped || record.socket !== socket) return;
      if (qr) { record.qr = qr; record.status = 'qr_ready'; }
      if (connection === 'open') {
        record.status = 'ready';
        record.qr = '';
        const jid = socket.user?.id || '';
        record.phone = jid.split(':')[0].split('@')[0].replace(/\D/g, '');
      }
      if (connection === 'close') {
        record.status = 'disconnected'; record.qr = '';
        const code = lastDisconnect?.error?.output?.statusCode;
        record.socket = null;
        if (code === DisconnectReason.loggedOut) {
          record.status = 'action_required';
          record.stopped = true;
        } else if (!record.stopped) {
          record.reconnect = setTimeout(() => {
            if (!record.stopped) void startSession(id).catch(() => { record.status = 'failed'; });
          }, 5000);
        }
      }
    });
    socket.ev.on('messages.upsert', async ({ messages, type }) => {
      if (type !== 'notify' || record.stopped) return;
      for (const m of messages || []) {
        const jid = m.key?.remoteJid || '';
        const from = jid.endsWith('@s.whatsapp.net') ? jid.split('@')[0] : '';
        if (!validPhone(from) || m.key?.fromMe || !m.key?.id || !m.message) continue;
        const msgType = getContentType(m.message);
        let text = '';
        if (msgType === 'conversation') text = m.message.conversation || '';
        else if (msgType === 'extendedTextMessage') text = m.message.extendedTextMessage?.text || '';
        else if (msgType === 'imageMessage') text = m.message.imageMessage?.caption || '[Imagem recebida]';
        else if (msgType === 'videoMessage') text = m.message.videoMessage?.caption || '[Vídeo recebido]';
        else if (msgType === 'audioMessage') text = '[Áudio recebido]';
        else if (msgType === 'documentMessage') text = '[Documento recebido]';
        else continue;
        try { await queueEvent(id, m.key.id, from, text.slice(0, 4096), msgType); }
        catch { record.status = 'failed'; record.lastError = 'outbox-write'; }
      }
    });
  } catch {
    record.stopped = true;
    record.status = 'failed';
    record.socket = null;
    throw new Error('Could not initialize session');
  }
  return record;
}
async function stopSession(id) {
  const s = gatewaySessions.get(id);
  if (!s) throw new Error('Session not found');
  s.stopped = true;
  if (s.reconnect) clearTimeout(s.reconnect);
  s.reconnect = null;
  if (s.socket) { const sock = s.socket; s.socket = null; try { sock.end(undefined); } catch { /* benign */ } }
  s.qr = ''; s.status = 'disconnected';
  return s;
}
function publicStatus(s) {
  return { id: s.id, status: s.status, phone: s.phone, engineLoaded: !!s.socket };
}
function sendJSON(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'content-type':'application/json; charset=utf-8',
    'cache-control':'no-store', 'x-content-type-options':'nosniff' });
  res.end(body);
}
async function requestBody(req) {
  const chunks = []; let size = 0;
  for await (const c of req) {
    size += c.length;
    if (size > MAX_BODY) throw new Error('Payload too large');
    chunks.push(c);
  }
  const parsed = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('Invalid payload');
  return parsed;
}
const allowedMethods = new Map([
  ['/v1/sessions', ['GET', 'POST']],
]);
const server = http.createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/healthz') return sendJSON(res, 200, { ok: true });
  if (!equalSecret(req.headers.authorization || '', 'Bearer ' + sharedKey)) return sendJSON(res, 401, { error:'unauthorized' });
  const pathname = req.url || '';
  if (pathname.length > 150 || pathname.includes('?')) return sendJSON(res, 404, { error:'not_found' });
  try {
    if (pathname === '/v1/sessions' && req.method === 'GET')
      return sendJSON(res, 200, [...gatewaySessions.values()].map(publicStatus));
    if (pathname === '/v1/sessions' && req.method === 'POST') {
      const body = await requestBody(req);
      if (!validSessionId(body.id)) return sendJSON(res, 400, { error:'invalid_session' });
      const record = await createSession(body.id);
      return sendJSON(res, 201, publicStatus(record));
    }
    const match = /^\/v1\/sessions\/([A-Za-z0-9-]{3,80})(?:\/(start|stop|qr|send))?$/.exec(pathname);
    if (!match) return sendJSON(res, 404, { error:'not_found' });
    const [, id, operation] = match;
    const record = gatewaySessions.get(id);
    if (!record) return sendJSON(res, 404, { error:'session_not_found' });
    if (!operation && req.method === 'GET') return sendJSON(res, 200, publicStatus(record));
    if (operation === 'start' && req.method === 'POST')
      return sendJSON(res, 200, publicStatus(await startSession(id)));
    if (operation === 'stop' && req.method === 'POST')
      return sendJSON(res, 200, publicStatus(await stopSession(id)));
    if (operation === 'qr' && req.method === 'GET') {
      if (record.status !== 'qr_ready' || !record.qr) return sendJSON(res, 404, { error:'qr_not_available' });
      const qrDataUrl = await QRCode.toDataURL(record.qr, { width: 300, margin: 2 });
      return sendJSON(res, 200, { id, qrDataUrl });
    }
    if (operation === 'send' && req.method === 'POST') {
      const body = await requestBody(req);
      if (!validPhone(body.phone) || typeof body.text !== 'string' ||
          !body.text.trim() || body.text.length > 4096) return sendJSON(res, 400, { error:'invalid_message' });
      if (record.status !== 'ready' || !record.socket) return sendJSON(res, 409, { error:'not_ready' });
      const result = await record.socket.sendMessage(body.phone + '@s.whatsapp.net', { text: body.text });
      if (!result?.key?.id) return sendJSON(res, 502, { error:'send_not_confirmed' });
      return sendJSON(res, 201, { messageId: 'swa:' + id + ':' + result.key.id });
    }
    return sendJSON(res, 405, { error:'method_not_allowed' });
  } catch {
    return sendJSON(res, 503, { error:'operation_unavailable' });
  }
});
for (const id of await persistedSessionIds(store)) await createSession(id);
server.listen(port, '0.0.0.0');
setInterval(() => { void flushOutbox(); }, 15000).unref();
if (process.env.WA_AUTOSTART === 'true')
  for (const id of gatewaySessions.keys()) void startSession(id).catch(() => {});
process.on('SIGTERM', () => { server.close(); for (const id of gatewaySessions.keys()) void stopSession(id); });
