// Baileys-based helper to initialize WhatsApp, emit QR as data URL, and exit deterministically
// Usage: node wa_baileys_helper.js <sessionId> [phoneNumber]

const path = require('path');
const fs = require('fs');
const qrcode = require('qrcode');
const axios = require('axios');
const http = require('http');
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, downloadContentFromMessage } = require('@whiskeysockets/baileys');

// Configuration
const BACKEND_URL = process.env.BACKEND_URL || 'http://127.0.0.1:8000';
const BRIDGE_HOST = process.env.BRIDGE_HOST || '127.0.0.1';

// Import message buffer for batch processing
const MessageBuffer = require('./message_buffer');
const r2_cloud = require('./r2_uploader');
const messageBuffer = new MessageBuffer(100, 5000, BACKEND_URL); // batch size: 100, flush interval: 5s, pass dynamic URL

const sessionId = process.argv[2];
const initialArgPhone = process.argv[3];

const runId = process.env.WA_STREAM_RUN_ID || `${sessionId}_${process.pid}_${Date.now()}`;

let lastConnectionUpdateAt = null;
let lastSocketOpenAt = null;
let lastSocketCloseAt = null;
let lastMessagesUpsertAt = null;
let lastMessageBufferedAt = null;
let messageUpsertCount = 0;
let lastDisconnectCode = null;
let lastDisconnectReason = null;
let reconnectCount = 0;

function nowIso() {
  return new Date().toISOString();
}

function buildHealthPayload(extra = {}) {
  return {
    session_id: sessionId,
    run_id: runId,
    pid: process.pid,

    socket_ready: !!sock,
    connection_state: currentConnectionState,
    ws_ready_state: sock?.ws?.readyState ?? null,
    is_connected: currentConnectionState === "open",

    phone_number: waNumber || null,
    user_id: sock?.user?.id || null,

    last_connection_update_at: lastConnectionUpdateAt,
    last_socket_open_at: lastSocketOpenAt,
    last_socket_close_at: lastSocketCloseAt,

    last_messages_upsert_at: lastMessagesUpsertAt,
    last_message_buffered_at: lastMessageBufferedAt,
    message_upsert_count: messageUpsertCount,

    last_disconnect_code: lastDisconnectCode,
    last_disconnect_reason: lastDisconnectReason,
    reconnect_count: reconnectCount,

    uptime_sec: Math.floor(process.uptime()),
    ...extra
  };
}

const api = axios.create({
  baseURL: `${BACKEND_URL}/connector/api`,
  withCredentials: true, // Important for CORS with credentials
  headers: {
    'Content-Type': 'application/json',
    'Accept': 'application/json'
  }
});

if (!sessionId) {
  console.log(JSON.stringify({ status: 'error', error: 'Session ID is required' }));
  process.exit(1);
}
// Will be populated after connection opens if not provided.
let waNumber = initialArgPhone || null;

// Safe stringify to inspect Baileys payloads
function getCircularReplacer() {
  const seen = new WeakSet();
  return (key, value) => {
    if (typeof value === 'bigint') { return value.toString(); }
    if (typeof value === 'object' && value !== null) {
      if (seen.has(value)) return '[Circular]';
      seen.add(value);
    }
    return value;
  };
}

function logDebug(label, obj) {
  try {
    console.log(JSON.stringify({ status: 'debug', label, payload: obj }, getCircularReplacer()));
  } catch (_) { }
}

// ensureWwebCompat removed as we only rely on Baileys creds.json now

const getParticipantJid = (msg) => {
  return msg?.key?.participant || msg?.participant || msg?.message?.key?.participant || null;
};

const getParticipantAltJid = (msg) => {
  return msg?.key?.participantAlt || null;
};

let sock;
let bridgeServerStarted = false;
let bridgeServer = null;
let currentConnectionState = 'close';

let helperAuthFolder = null;
let portFilePath = null;

const cleanupBridgePortFile = () => {
  try {
    if (portFilePath && fs.existsSync(portFilePath)) {
      fs.unlinkSync(portFilePath);
      console.log(JSON.stringify({ status: 'info', event: 'bridge_port_file_removed' }));
    }
  } catch (e) {
    console.log(JSON.stringify({
      status: 'warn',
      event: 'bridge_port_file_remove_failed',
      error: e.message
    }));
  }
};

const bareNumberFromJid = (jid) => {
  if (!jid) return '';
  const s = String(jid);
  if (s.endsWith('@lid') || s.endsWith('@g.us')) return s.split('@')[0];
  return s.split('@')[0].split(':')[0].split('-')[0];
};

const getPNForLID = async (lid) => {
  if (!lid || !sock?.signalRepository?.lidMapping?.getPNForLID) return null;
  try {
    const pnJid = await sock.signalRepository.lidMapping.getPNForLID(lid);
    return pnJid ? bareNumberFromJid(pnJid) : null;
  } catch {
    return null;
  }
};

const resolveParticipantPhone = async (participantJid) => {
  if (!participantJid) return null;

  if (participantJid.endsWith('@s.whatsapp.net')) {
    return bareNumberFromJid(participantJid);
  }

  if (participantJid.endsWith('@lid')) {
    // 1. Try local Baileys cache
    const cachedPn = await getPNForLID(participantJid);
    if (cachedPn) return cachedPn;

    // 2. Try Backend Database (our persistent store)
    try {
      const response = await api.get(`/whatsapp/lid-mappings/${encodeURIComponent(participantJid)}`);
      if (response?.data?.phone_number) {
        return response.data.phone_number;
      }
    } catch (err) {
      // logger.debug(`Failed to resolve LID from DB: ${err.message}`);
    }

    // 3. Fallback: return null if no phone mapping found
    return null;
  }

  return null;
};

// Clean text similar to wa_helper.js
function cleanMessageContent(text) {
  if (!text || typeof text !== 'string') return '';
  return text
    .replace(/\[?media omitted\]?/gi, '')
    .replace(/[\r\n]+/g, ' ') // Replace newlines with space to prevent word merging
    .replace(/([\u2700-\u27BF]|[\uE000-\uF8FF]|\uD83C[\uDC00-\uDFFF]|\uD83D[\uDC00-\uDFFF]|\uD83E[\uDD00-\uDDFF])/g, '')
    .replace(/[\x00-\x1F\x7F-\x9F]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function _safeExtFromMime(mime) {
  if (!mime || typeof mime !== 'string') return '';
  const m = mime.toLowerCase();
  if (m.includes('jpeg')) return '.jpeg';
  if (m.includes('jpg')) return '.jpg';
  if (m.includes('png')) return '.png';
  if (m.includes('webp')) return '.webp';
  if (m.includes('gif')) return '.gif';
  if (m.includes('mp4')) return '.mp4';
  if (m.includes('mpeg')) return '.mpeg';
  if (m.includes('mp3')) return '.mp3';
  if (m.includes('ogg')) return '.ogg';
  if (m.includes('pdf')) return '.pdf';
  return '';
}

function _getMediaNodeAndType(msg) {
  const m = msg?.message;
  if (!m) return { node: null, type: null };
  if (m.imageMessage) return { node: m.imageMessage, type: 'image' };
  if (m.videoMessage) return { node: m.videoMessage, type: 'video' };
  if (m.audioMessage) return { node: m.audioMessage, type: 'audio' };
  if (m.documentMessage) return { node: m.documentMessage, type: 'document' };
  return { node: null, type: null };
}

/**
 * Validates if a message should be stored in the database.
 * Skips status broadcasts, protocol messages, stub/system messages, and messages with no content/media.
 */
function isValidMessage(msg) {
  if (!msg) return false;

  // 1. Skip status broadcasts
  const remoteJid = msg.key?.remoteJid || '';
  const remoteJidAlt = msg.key?.remoteJidAlt || '';
  if (remoteJid === 'status@broadcast' || remoteJidAlt === 'status@broadcast') return false;

  // 2. Skip protocol messages (ephemeral, etc)
  if (msg.message?.protocolMessage) return false;

  // 3. Skip stub/system messages (like E2E encryption notice)
  if (msg.messageStubType) return false;

  // 4. Check for content
  const text = msg.message?.conversation ||
    msg.message?.extendedTextMessage?.text ||
    msg.message?.imageMessage?.caption ||
    '';

  const mediaInfo = _getMediaNodeAndType(msg);
  const hasMedia = !!mediaInfo?.node;

  if (!text && !hasMedia) return false;

  return true;
}

async function _downloadMessageMedia(sessionId, messageId, msg) {
  try {
    const { node, type } = _getMediaNodeAndType(msg);
    if (!node || !type) return null;

    const mime = node?.mimetype;
    const ext = _safeExtFromMime(mime) || '';

    const stream = await downloadContentFromMessage(node, type);
    const chunks = [];
    for await (const chunk of stream) {
      chunks.push(chunk);
    }
    const buffer = Buffer.concat(chunks);

    return { buffer, ext, type };
  } catch (e) {
    try { console.log(JSON.stringify({ status: 'warn', event: 'media_download_failed', message_id: messageId, error: e.message })); } catch (_) { }
    return null;
  }
}

async function _uploadMediaToR2(sessionId, messageId, mediaBuffer, mediaType, extension) {
  if (!mediaBuffer) return null;
  try {
    const safeMessageId = String(messageId || Date.now()).replace(/[^\w.-]/g, '_');
    const safeMediaType = String(mediaType || 'media').replace(/[^\w.-]/g, '_');
    const r2res = await r2_cloud.uploadToR2({
      sourceUrlOrPath: mediaBuffer,
      key: `whatsapp/${safeMediaType}/${safeMessageId}${extension || ''}`,
    });
    return r2res?.url || r2res?.key || null;
  } catch (_) {
    return null;
  }
}

process.on('uncaughtException', async (err) => {
  // If it's an Axios error, it's likely a temporary API outage. Don't crash the bridge.
  if (err.isAxiosError) {
    console.log(JSON.stringify({ status: 'warn', event: 'uncaughtException_axios', error: err.message }));
    return;
  }
  try { console.log(JSON.stringify({ status: 'error', error: `uncaughtException: ${err.message}` })); } catch (_) { }
  try { await messageBuffer.stop(); } catch (_) { }
  try { cleanupBridgePortFile(); } catch (_) { }
  process.exit(1);
});

process.on('unhandledRejection', async (reason) => {
  // If it's an Axios error, it's likely a temporary API outage. Don't crash the bridge.
  if (reason && reason.isAxiosError) {
    console.log(JSON.stringify({ status: 'warn', event: 'unhandledRejection_axios', error: reason.message }));
    return;
  }
  try {
    const msg = (reason && reason.message) ? reason.message : String(reason);
    console.log(JSON.stringify({ status: 'error', error: `unhandledRejection: ${msg}` }));
  } catch (_) { }
  try { await messageBuffer.stop(); } catch (_) { }
  try { cleanupBridgePortFile(); } catch (_) { }
  process.exit(1);
});

// Graceful shutdown on signal
const shutdown = async (signal) => {
  console.log(JSON.stringify({ status: 'info', event: 'shutdown_signal_received', signal }));
  try { await messageBuffer.stop(); } catch (_) { }
  try { cleanupBridgePortFile(); } catch (_) { }
  process.exit(0);
};
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

const main = async () => {
  const sessionId = process.argv[2] || 'default';
  const groupCache = new Map();
  const sanitizeNumber = (n) => {
    if (!n) return null;
    // If a list of numbers is passed accidentally (comma-separated), pick the first.
    // Otherwise sanitizing would remove commas and concatenate digits.
    const first = String(n).split(',')[0];
    // take only part before ':' and '-' and non-digits
    let s = String(first).split('@')[0];
    s = s.split(':')[0];
    s = s.split('-')[0];
    return s.replace(/\D/g, '');
  };
  let waNumber = sanitizeNumber(process.argv[3] || null);

  if (!sessionId) {
    try { console.log(JSON.stringify({ status: 'error', error: 'Session ID is required.' })); } catch (_) { }
    process.exit(1);
  }

  const baseDir = __dirname; // current script dir
  const authFolder = path.join(baseDir, '.baileys_auth', sessionId);
  helperAuthFolder = authFolder;
  portFilePath = path.join(authFolder, 'bridge_port.txt');

  // Ensure auth folder exists
  try {
    if (!fs.existsSync(authFolder)) {
      fs.mkdirSync(authFolder, { recursive: true });
      try { console.log(JSON.stringify({ status: 'info', event: 'auth_folder_created', authFolder })); } catch (_) { }
    }
    // Optional: purge stale app-state files to avoid bad decrypt (controlled via env)
    try {
      const shouldPurge = String(process.env.BAILEYS_PURGE_APP_STATE_ON_START || 'false').toLowerCase() === 'true';
      if (shouldPurge) {
        const files = fs.readdirSync(authFolder);
        let purged = 0;
        for (const f of files) {
          if (f.startsWith('app-state-') && f.endsWith('.json')) {
            try { fs.unlinkSync(path.join(authFolder, f)); purged++; } catch (_) { }
          }
        }
        if (purged > 0) console.log(JSON.stringify({ status: 'warn', event: 'purged_app_state', count: purged }));
      }
    } catch (e) {
      console.log(JSON.stringify({ status: 'warn', event: 'purge_app_state_failed', error: e.message }));
    }
    // ensureWwebCompat removed
  } catch (e) {
    try { console.log(JSON.stringify({ status: 'error', error: 'Failed to prepare auth folder: ' + e.message, authFolder })); } catch (_) { }
  }

  // Heartbeat to keep session updated in DB
  const startHeartbeat = () => {
    setInterval(async () => {
      try {
        await api.post(
          `/whatsapp/session/${sessionId}/heartbeat`,
          buildHealthPayload({ event_source: "interval_heartbeat" })
        );
      } catch (e) {
        console.log(JSON.stringify({
          status: "warn",
          event: "heartbeat_failed",
          error: e.message
        }));
      }
    }, 30000);
  };
  startHeartbeat();

  const { state, saveCreds } = await useMultiFileAuthState(authFolder);
  const { version } = await fetchLatestBaileysVersion();

  // Track last QR string to re-emit fresh QR codes; WhatsApp QR expires every ~20s
  let lastQr = null;
  let retryCount = 0;
  const maxRetries = 5;

  // Collect chat JIDs for optional deep backfill
  const chatJids = new Set();

  // Backfill: process all history batches Baileys emits via messages.set
  const BACKFILL_PER_CHAT = 2000; // cap per chat
  const BACKFILL_TOTAL = 20000;   // global cap
  const backfillCounts = new Map();
  let backfillTotal = 0;


  function getRemoteJid(msg) {
    return msg?.key?.remoteJidAlt || msg?.key?.remoteJid || null;
  }
  async function resolvePeerNumber(msg) {
    if (!msg || !msg.key) return '';
    const remoteJid = msg.key.remoteJid || '';
    const remoteJidAlt = msg.key.remoteJidAlt || '';

    // Symmetry check: Learn mapping if we have both LID and PN
    const learnMapping = (lid, pn) => {
      if (lid && lid.endsWith('@lid') && pn && pn.endsWith('@s.whatsapp.net')) {
        api.post('/whatsapp/lid-mappings', {
          admin_number: getAdminNumber(msg),
          mappings: { [lid]: pn }
        }).catch(() => { });
      }
    };

    // Case 1: remoteJid is PN, remoteJidAlt is LID (addressingMode: 'pn')
    // Case 2: remoteJid is LID, remoteJidAlt is PN (addressingMode: 'lid')

    if (remoteJid.endsWith('@s.whatsapp.net')) {
      if (remoteJidAlt.endsWith('@lid')) learnMapping(remoteJidAlt, remoteJid);
      return bareNumberFromJid(remoteJid);
    }

    if (remoteJidAlt.endsWith('@s.whatsapp.net')) {
      if (remoteJid.endsWith('@lid')) learnMapping(remoteJid, remoteJidAlt);
      return bareNumberFromJid(remoteJidAlt);
    }

    // Standard group/LID resolution if no immediate PN in key
    if (remoteJid.endsWith('@g.us')) return bareNumberFromJid(remoteJid);

    if (remoteJid.endsWith('@lid')) {
      // PRIORITY 2: Check Baileys internal signal repository
      const pn = await getPNForLID(remoteJid);
      if (pn) return pn;

      // PRIORITY 3: Check our historical DB mappings
      const dbResolved = await resolveParticipantPhone(remoteJid);
      if (dbResolved && dbResolved !== remoteJid) return dbResolved;

      // FALLBACK: Return bare LID
      return bareNumberFromJid(remoteJid);
    }

    return bareNumberFromJid(remoteJid);
  }

  // Try to determine our own WhatsApp number early, even before 'open'
  function getAdminNumber(msg) {
    // Cached/session-provided
    let admin = waNumber || '';
    if (admin) return admin;
    // From active socket user
    admin = bareNumberFromJid(sock?.user?.id);
    if (admin) { waNumber = waNumber || admin; return admin; }
    // From credentials (available after auth state loads)
    try { admin = bareNumberFromJid(state?.creds?.me?.id); } catch (_) { }
    if (admin) { waNumber = waNumber || admin; return admin; }
    return '';
  }

  let badDecryptRecovered = false;

  const purgeAppState = () => {
    try {
      const files = fs.readdirSync(authFolder);
      let purged = 0;
      for (const f of files) {
        if (f.startsWith('app-state-') && f.endsWith('.json')) {
          try { fs.unlinkSync(path.join(authFolder, f)); purged++; } catch (_) { }
        }
      }
      if (purged > 0) console.log(JSON.stringify({ status: 'warn', event: 'purged_app_state_auto', count: purged }));
    } catch (e) {
      console.log(JSON.stringify({ status: 'warn', event: 'purge_app_state_auto_failed', error: e.message }));
    }
  };

  const startBridgeServer = () => {
    if (bridgeServerStarted) return;
    bridgeServerStarted = true;

    bridgeServer = http.createServer((req, res) => {
      if (req.method === 'GET' && req.url === '/health') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({
          success: true,
          ...buildHealthPayload()
        }));
      }

      if (req.method === 'POST' && req.url === '/stop') {
        let body = '';
        req.on('data', chunk => { body += chunk.toString(); });
        req.on('end', async () => {
          try {
            const parsed = JSON.parse(body || '{}');
            const logout = parsed.logout === true;

            // Drain message buffer before exiting
            try { await messageBuffer.stop(); } catch (_) { }

            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ success: true, message: logout ? 'Logging out...' : 'Stopping...' }));

            console.log(JSON.stringify({ status: 'info', event: 'graceful_stop_requested', logout }));

            // Safety exit timeout in case logout hangs
            const safetyExit = setTimeout(() => {
              console.log(JSON.stringify({ status: 'warn', event: 'graceful_stop_timeout', message: 'Forcing exit' }));
              cleanupBridgePortFile();
              process.exit(0);
            }, 5000);

            if (logout && sock) {
              try {
                await sock.logout();
                console.log(JSON.stringify({ status: 'info', event: 'logout_success' }));
              } catch (e) {
                console.log(JSON.stringify({ status: 'warn', event: 'logout_error', error: e.message }));
              }
            }
            if (sock) {
              try { sock.end(); } catch (e) { }
            }

            // Cleanup and exit properly
            clearTimeout(safetyExit);
            setTimeout(() => {
              cleanupBridgePortFile();
              process.exit(0);
            }, 500);
          } catch (e) {
            res.writeHead(500, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ success: false, error: e.message }));
          }
        });
        return;
      }

      if (req.method === 'POST' && req.url === '/send') {
        let body = '';

        req.on('data', chunk => {
          body += chunk.toString();
        });

        req.on('end', async () => {
          try {
            const parsed = JSON.parse(body || '{}');
            const { jid, text, quoted, mentions } = parsed;

            if (!jid || !text) {
              res.writeHead(400, { 'Content-Type': 'application/json' });
              return res.end(JSON.stringify({
                success: false,
                error: 'jid and text are required'
              }));
            }

            if (!sock) {
              res.writeHead(503, { 'Content-Type': 'application/json' });
              return res.end(JSON.stringify({
                success: false,
                error: 'Socket not initialized'
              }));
            }

            if (currentConnectionState !== 'open') {
              res.writeHead(503, { 'Content-Type': 'application/json' });
              return res.end(JSON.stringify({
                success: false,
                error: `WhatsApp connection is not open (state=${currentConnectionState})`
              }));
            }

            const result = await sock.sendMessage(
              jid,
              { text, mentions: Array.isArray(mentions) ? mentions : undefined },
              quoted ? { quoted } : {}
            );

            res.writeHead(200, { 'Content-Type': 'application/json' });
            return res.end(JSON.stringify({
              success: true,
              status: 'sent',
              result: {
                key: result?.key || null,
                timestamp: result?.messageTimestamp || null
              }
            }));
          } catch (e) {
            res.writeHead(500, { 'Content-Type': 'application/json' });
            return res.end(JSON.stringify({
              success: false,
              error: e.message
            }));
          }
        });

        return;
      }

      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        success: false,
        error: 'Not found'
      }));
    });

    bridgeServer.listen(0, BRIDGE_HOST, () => {
      try {
        const addr = bridgeServer.address();
        const port = addr?.port;
        console.log(JSON.stringify({ status: 'info', event: 'bridge_server_started', port }));

        fs.writeFileSync(portFilePath, String(port));
      } catch (e) {
        console.log(JSON.stringify({
          status: 'warn',
          event: 'failed_to_write_port_file',
          error: e.message,
          authFolder
        }));
      }
    });
  };

  const connect = async () => {
    // Use a loop to retry on transient errors
    if (retryCount >= maxRetries) {
      try { console.log(JSON.stringify({ status: 'error', error: 'Max retries reached, exiting.' })); } catch (_) { }
      process.exit(1);
    }

    // No MongoDB connection needed - using HTTP API instead
    console.log(JSON.stringify({ status: 'info', event: 'using_http_api' }));

    sock = makeWASocket({
      version,
      auth: state,
      printQRInTerminal: false,
      browser: ['Win', 'Chrome', '121.0'],
      // enable history so we can backfill like wa_helper.js
      syncFullHistory: true,
      cachedGroupMetadata: async (jid) => {
        const cached = groupCache.get(jid);
        if (cached && (Date.now() - cached.timestamp < 300000)) {
          return cached.data;
        }
        const meta = await sock.groupMetadata(jid);
        groupCache.set(jid, { data: meta, timestamp: Date.now() });
        return meta;
      }
    });


    // Register chats.set listener after sock is created
    sock.ev.on('chats.set', (ev) => {
      try {
        const list = Array.isArray(ev?.chats) ? ev.chats : [];
        for (const c of list) {
          if (c?.id) chatJids.add(c.id);
        }
        console.log(JSON.stringify({ status: 'info', event: 'chats.set', count: list.length }));
      } catch (_) { }
    });
    // Also gather chat IDs from upserts/updates in case chats.set doesn't fire
    sock.ev.on('chats.upsert', (ev) => {
      try {
        const list = Array.isArray(ev) ? ev : (Array.isArray(ev?.chats) ? ev.chats : []);
        for (const c of list) { if (c?.id) chatJids.add(c.id); }
        console.log(JSON.stringify({ status: 'info', event: 'chats.upsert', count: list.length }));
      } catch (_) { }
    });
    sock.ev.on('chats.update', (ev) => {
      try {
        const list = Array.isArray(ev) ? ev : (Array.isArray(ev?.chats) ? ev.chats : []);
        for (const c of list) { if (c?.id) chatJids.add(c.id); }
        console.log(JSON.stringify({ status: 'info', event: 'chats.update', count: list.length }));
      } catch (_) { }
    });

    // NEW v7 LID and Group Listeners
    sock.ev.on('contacts.upsert', async (contacts) => {
      try {
        const admin_number = getAdminNumber();
        if (!admin_number) return;

        const lidMap = {};
        for (const contact of contacts) {
          if (contact.id && contact.lid && contact.id.endsWith('@s.whatsapp.net')) {
            lidMap[contact.lid] = contact.id;
          }
          await api.post('/whatsapp/contact', { admin_number, ...contact }).catch(() => { });
        }

        if (Object.keys(lidMap).length > 0) {
          console.log(JSON.stringify({ status: 'info', event: 'mappings_learned_from_contacts_upsert', count: Object.keys(lidMap).length }));
          await api.post('/whatsapp/lid-mappings', { admin_number, mappings: lidMap }).catch(() => { });
        }
        console.log(JSON.stringify({ status: 'info', event: 'contacts_upserted', count: contacts.length }));
      } catch (e) {
        console.log(JSON.stringify({ status: 'error', event: 'contacts_upsert_failed', error: e.message }));
      }
    });

    sock.ev.on('contacts.update', async (updates) => {
      try {
        const admin_number = getAdminNumber();
        if (!admin_number) return;

        const lidMap = {};
        for (const update of updates) {
          if (update.id && update.lid && update.id.endsWith('@s.whatsapp.net')) {
            lidMap[update.lid] = update.id;
          }
          await api.post('/whatsapp/contact', { admin_number, ...update }).catch(() => { });
        }

        if (Object.keys(lidMap).length > 0) {
          console.log(JSON.stringify({ status: 'info', event: 'mappings_learned_from_contacts_update', count: Object.keys(lidMap).length }));
          await api.post('/whatsapp/lid-mappings', { admin_number, mappings: lidMap }).catch(() => { });
        }
      } catch (e) { }
    });

    sock.ev.on('lid-mapping.update', async (mappings) => {
      try {
        const admin_number = getAdminNumber();
        if (!admin_number) return;
        const map = {};
        for (const m of mappings) {
          if (m.lid && m.phoneNumber) {
            const rawLid = m.lid;
            const cleanPn = bareNumberFromJid(m.phoneNumber);
            if (rawLid && cleanPn) {
              map[rawLid] = cleanPn;
            }
          }
        }
        if (Object.keys(map).length > 0) {
          await api.post('/whatsapp/lid-mappings', { admin_number, mappings: map });
          console.log(JSON.stringify({ status: 'info', event: 'lid_mapping_synced', count: Object.keys(map).length }));
        }
      } catch (e) {
        console.log(JSON.stringify({ status: 'warn', event: 'lid_mapping_sync_failed', error: e.message }));
      }
    });

    sock.ev.on('groups.upsert', async (groups) => {
      try {
        const admin_number = getAdminNumber();
        if (!admin_number) return;
        for (const group of groups) {
          await api.post('/whatsapp/group', { admin_number, ...group });
        }
        console.log(JSON.stringify({ status: 'info', event: 'groups_upserted', count: groups.length }));
      } catch (e) {
        console.log(JSON.stringify({ status: 'warn', event: 'groups_sync_failed', error: e.message }));
      }
    });


    sock.ev.on('creds.update', async () => {
      try {
        await saveCreds();
        // After saving, check file size for diagnostics
        try {
          const credsPath = path.join(authFolder, 'creds.json');
          const stats = fs.statSync(credsPath);
          console.log(JSON.stringify({ status: 'info', event: 'creds_saved', path: credsPath, size: stats.size }));
        } catch (e) {
          console.log(JSON.stringify({ status: 'info', event: 'creds_saved_stat_error', error: e.message }));
        }
      } catch (e) {
        console.log(JSON.stringify({ status: 'error', event: 'creds_save_failed', error: e.message }));
      }
    });

    sock.ev.on('connection.update', async (update) => {
      const { connection, lastDisconnect, qr } = update;

      lastConnectionUpdateAt = nowIso();

      if (connection) {
        currentConnectionState = connection;
      }

      if (connection === "open") {
        lastSocketOpenAt = nowIso();
        lastDisconnectCode = null;
        lastDisconnectReason = null;

        await api.post("/whatsapp/session/event", buildHealthPayload({
          event: "connected",
          phoneNumber: waNumber,
          statusCode: null,
          reason: null,
        })).catch(() => {});
      }

      if (connection === "close") {
        const statusCode = lastDisconnect?.error?.output?.statusCode;
        const errMsg =
          (lastDisconnect?.error && (lastDisconnect.error.message || String(lastDisconnect.error))) || "";

        lastSocketCloseAt = nowIso();
        lastDisconnectCode = statusCode || null;
        lastDisconnectReason = errMsg;
        reconnectCount++;

        const isLoggedOut = statusCode === DisconnectReason.loggedOut;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

        let eventName = "disconnected";
        if (isLoggedOut) eventName = "logged_out";
        else if (shouldReconnect) eventName = "reconnecting";

        await api.post("/whatsapp/session/event", buildHealthPayload({
          event: eventName,
          statusCode,
          reason: errMsg,
        })).catch(() => {});
      }

      console.log(JSON.stringify({ status: 'info', event: 'connection.update', connection: currentConnectionState }));

      if (qr && qr !== lastQr) {
        try {
          const qrImage = await qrcode.toDataURL(qr);
          console.log(JSON.stringify({ status: 'qr', qrCode: qrImage, sessionId, phoneNumber: waNumber }));
          lastQr = qr;
          // Persist latest QR for polling endpoints (helps frontend refresh QR as it rotates)
          try {
            const qrFile = path.join(__dirname, '.baileys_auth', sessionId, 'last_qr.txt');
            fs.writeFileSync(qrFile, qrImage);
          } catch (_) { /* ignore file write issues */ }
        } catch (e) {
          console.log(JSON.stringify({ status: 'error', error: 'QR generation failed: ' + e.message }));
        }
      }

      if (connection === 'open') {
        retryCount = 0; // reset on success
        const meJid = (sock && sock.user && sock.user.id) || (state && state.creds && state.creds.me && state.creds.me.id) || '';
        const derived = bareNumberFromJid(meJid);
        if (derived) waNumber = waNumber || derived;
        console.log(JSON.stringify({ status: 'ready', sessionId, phoneNumber: waNumber }));

        // Force save creds on open, as 'creds.update' may not fire if unchanged
        try {
          await saveCreds();
          console.log(JSON.stringify({ status: 'info', event: 'creds_forced_save_on_open' }));
        } catch (e) {
          console.log(JSON.stringify({ status: 'error', event: 'creds_forced_save_failed', error: e.message }));
        }

        // (Backend notified via connection.update top block)

        // Fetch and sync groups immediately since history sync may have already passed
        try {
          const admin_number = getAdminNumber();
          if (admin_number && typeof sock.groupFetchAllParticipating === 'function') {
            const groups = await sock.groupFetchAllParticipating();
            const groupList = Object.values(groups);
            console.log(JSON.stringify({ status: 'info', event: 'forcing_group_sync', count: groupList.length }));
            for (const grp of groupList) {
              await api.post('/whatsapp/group', { admin_number, ...grp });
            }
          }
        } catch (e) {
          console.log(JSON.stringify({ status: 'warn', event: 'force_group_sync_failed', error: e.message }));
        }

        // Optional deep backfill using pagination (env-controlled)
        try {
          const enabled = String(process.env.DEEP_BACKFILL_ENABLED || 'false').toLowerCase() === 'true';
          if (enabled) {
            const perChatCap = parseInt(process.env.DEEP_BACKFILL_MAX_PER_CHAT || '2000', 10);
            const totalCap = parseInt(process.env.DEEP_BACKFILL_TOTAL || '20000', 10);
            const batchSize = parseInt(process.env.DEEP_BACKFILL_BATCH || '50', 10);

            const supportsFetch = typeof sock.fetchMessages === 'function' || typeof sock.loadMessages === 'function';
            if (!supportsFetch) {
              console.log(JSON.stringify({ status: 'warn', event: 'deep_backfill_unsupported' }));
            } else {
              console.log(JSON.stringify({ status: 'info', event: 'deep_backfill_start', perChatCap, totalCap, batchSize }));
              // Run without blocking event loop too long
              setTimeout(async () => {
                let globalCount = 0;
                for (const jid of Array.from(chatJids)) {
                  if (globalCount >= totalCap) break;
                  let fetchedForChat = 0;
                  let cursor = undefined; // Baileys accepts a cursor object; keep undefined to start from latest
                  try {
                    while (fetchedForChat < perChatCap && globalCount < totalCap) {
                      let msgs = [];
                      try {
                        // Some Baileys versions emit the initial history as 'messaging-history.set'
                        const count = Math.min(batchSize, perChatCap - fetchedForChat);
                        if (typeof sock.fetchMessages === 'function') {
                          msgs = await sock.fetchMessages(jid, count, cursor);
                        } else if (typeof sock.loadMessages === 'function') {
                          msgs = await sock.loadMessages(jid, count, cursor);
                        }
                      } catch (e) {
                        console.log(JSON.stringify({ status: 'warn', event: 'deep_backfill_fetch_error', jid, error: e.message }));
                        break;
                      }
                      if (!Array.isArray(msgs) || msgs.length === 0) break;

                      for (const msg of msgs) {
                        if (globalCount >= totalCap || fetchedForChat >= perChatCap) break;
                        if (!isValidMessage(msg)) continue;

                        const fromMe = !!msg?.key?.fromMe;
                        const id = (msg?.key?.id) || null;
                        const messageType = msg.message ? Object.keys(msg.message)[0] : null;
                        let text = (msg.message?.conversation || msg.message?.extendedTextMessage?.text || msg.message?.imageMessage?.caption || '') || '';
                        if (typeof text !== 'string') text = '';
                        const mediaInfo = _getMediaNodeAndType(msg);
                        const hasMedia = !!mediaInfo?.node;
                        const mediaNode = mediaInfo?.node || null;
                        const mediaKind = mediaInfo?.type || null;

                        const admin_number = getAdminNumber(msg);
                        const cx_number = await resolvePeerNumber(msg);
                        if (!admin_number || !cx_number || admin_number === "" || cx_number === "") {
                          continue;
                        }

                        const chatJid = msg?.key?.remoteJid || '';
                        const current = backfillCounts.get(chatJid) || 0;
                        if (current >= BACKFILL_PER_CHAT) { continue; }

                        const participantJid = getParticipantJid(msg);
                        const media = hasMedia ? await _downloadMessageMedia(sessionId, id, msg) : null;
                        const r2_media_url = (hasMedia && media) ? await _uploadMediaToR2(sessionId, id, media.buffer, mediaKind, media.ext) : null;
                        const doc = {
                          message_id: id || `${Date.now()}_${Math.random().toString(36).slice(2)}`,
                          direction: fromMe ? 'outgoing' : 'incoming',
                          admin_number, cx_number, content: text, clean_content: cleanMessageContent(text),
                          timestamp: new Date((msg.messageTimestamp || Math.floor(Date.now() / 1000)) * 1000),
                          device: 'baileys', issent: fromMe, isread: false, message_type: messageType,
                          media_mime: mediaNode?.mimetype || null,
                          media_size: mediaNode?.fileLength || mediaNode?.fileLengthLow || null,
                          quoted_id: msg.message?.extendedTextMessage?.contextInfo?.stanzaId || msg.message?.imageMessage?.contextInfo?.stanzaId || null,
                          quoted_text: msg.message?.extendedTextMessage?.contextInfo?.quotedMessage?.conversation || null,
                          remote_jid: getRemoteJid(msg),
                          r2_media_url: r2_media_url || null,
                          raw: msg,  // Store the complete raw message object
                          participant: participantJid,
                          peer_pn: await resolveParticipantPhone(getParticipantAltJid(msg) || participantJid),
                        };

                        try {
                          // Buffer message for batch processing
                          await messageBuffer.add(doc);
                          fetchedForChat++;
                          globalCount++;
                        } catch (e) {
                          console.log(JSON.stringify({ status: 'warn', event: 'deep_backfill_buffer_error', jid, message_id: id, error: e.message }));
                          // Fallback to single POST
                          try {
                            await api.post('/whatsapp/messages', doc);
                          } catch (err) {
                            console.log(JSON.stringify({ status: 'error', event: 'deep_backfill_http_fallback_failed', jid, message_id: id, error: err.message }));
                          }
                        }
                      }

                      // Advance cursor: use the last message in the batch as the before-cursor
                      const last = msgs[msgs.length - 1];
                      if (!last) break;
                      cursor = { before: last.key }; // safe generic cursor shape across versions
                    }
                    console.log(JSON.stringify({ status: 'info', event: 'deep_backfill_chat_done', jid, fetchedForChat }));
                  } catch (e) {
                    console.log(JSON.stringify({ status: 'warn', event: 'deep_backfill_chat_error', jid, error: e.message }));
                  }
                }
                console.log(JSON.stringify({ status: 'info', event: 'deep_backfill_done', globalCount }));
              }, 2000);
            }
          }
        } catch (e) {
          console.log(JSON.stringify({ status: 'warn', event: 'deep_backfill_init_error', error: e.message }));
        }

      }

      if (connection === 'close') {
        const statusCode = lastDisconnect?.error?.output?.statusCode;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
        const isLoggedOut = statusCode === DisconnectReason.loggedOut;
        const errMsg = (lastDisconnect?.error && (lastDisconnect.error.message || String(lastDisconnect.error))) || '';
        const badDecrypt = /bad decrypt/i.test(errMsg) || /Provider routines::bad decrypt/i.test(errMsg);

        console.log(JSON.stringify({
          status: 'info',
          event: 'connection.close',
          shouldReconnect,
          isLoggedOut,
          badDecrypt,
          statusCode,
          error: lastDisconnect?.error
        }));

        // (Backend notified via connection.update top block)

        if (badDecrypt && !badDecryptRecovered) {
          badDecryptRecovered = true;
          purgeAppState();
          retryCount++;
          setTimeout(connect, 3000);
          return;
        }
        if (shouldReconnect) {
          retryCount++;
          setTimeout(connect, 5000); // wait 5s and reconnect
        } else {
          console.log(JSON.stringify({ status: 'info', event: 'connection.permanent_close', reason: 'logged_out' }));
          // on logged out, we should probably exit so the supervisor can see the PID is dead
          setTimeout(() => {
            cleanupBridgePortFile();
            process.exit(0);
          }, 2000);
        }
      }
    });

    // Backfill: process all history batches Baileys emits via messages.set

    sock.ev.on('messaging-history.set', async (payload) => {
      try {
        // Handle lidPnMappings if present in history sync
        if (payload?.lidPnMappings) {
          try {
            const admin_number = getAdminNumber();
            if (admin_number) {
              const map = {};
              for (const m of payload.lidPnMappings) { map[m.lid] = m.phoneNumber; }
              if (Object.keys(map).length > 0) {
                await api.post('/whatsapp/lid-mappings', { admin_number, mappings: map });
              }
            }
          } catch (e) { }
        }

        const chats = Array.isArray(payload?.chats) ? payload.chats : [];
        for (const c of chats) { if (c?.id) chatJids.add(c.id); }
        const arr = Array.isArray(payload?.messages) ? payload.messages : [];
        console.log(JSON.stringify({ status: 'info', event: 'messaging-history.set', chats: chats.length, messageCount: arr.length, isLatest: payload?.isLatest, syncType: payload?.syncType }));
        let count = 0;
        for (const msg of arr) {
          if (!isValidMessage(msg)) continue;
          if (backfillTotal >= BACKFILL_TOTAL) { continue; }

          const fromMe = !!msg?.key?.fromMe;
          const id = (msg?.key?.id) || null;
          const messageType = msg.message ? Object.keys(msg.message)[0] : null;
          let text = (msg.message?.conversation || msg.message?.extendedTextMessage?.text || msg.message?.imageMessage?.caption || '') || '';
          if (typeof text !== 'string') text = '';
          const mediaInfo = _getMediaNodeAndType(msg);
          const hasMedia = !!mediaInfo?.node;
          const mediaNode = mediaInfo?.node || null;
          const mediaKind = mediaInfo?.type || null;

          const admin_number = getAdminNumber(msg);
          const cx_number = await resolvePeerNumber(msg);
          if (!admin_number || !cx_number || admin_number === "" || cx_number === "") {
            continue;
          }

          const chatJid = msg?.key?.remoteJid || '';
          const current = backfillCounts.get(chatJid) || 0;
          if (current >= BACKFILL_PER_CHAT) { continue; }

          const participantJid = getParticipantJid(msg);
          const media = hasMedia ? await _downloadMessageMedia(sessionId, id, msg) : null;
          const r2_media_url = (hasMedia && media) ? await _uploadMediaToR2(sessionId, id, media.buffer, mediaKind, media.ext) : null;
          const doc = {
            message_id: id || `${Date.now()}_${Math.random().toString(36).slice(2)}`,
            direction: fromMe ? 'outgoing' : 'incoming',
            admin_number, cx_number, content: text, clean_content: cleanMessageContent(text),
            timestamp: new Date((msg.messageTimestamp || Math.floor(Date.now() / 1000)) * 1000),
            device: 'baileys', issent: fromMe, isread: false, message_type: messageType,
            media_mime: mediaNode?.mimetype || null,
            media_size: mediaNode?.fileLength || mediaNode?.fileLengthLow || null,
            quoted_id: msg.message?.extendedTextMessage?.contextInfo?.stanzaId || msg.message?.imageMessage?.contextInfo?.stanzaId || null,
            quoted_text: msg.message?.extendedTextMessage?.contextInfo?.quotedMessage?.conversation || null,
            remote_jid: getRemoteJid(msg),
            r2_media_url: r2_media_url || null,
            raw: msg,  // Store the complete raw message object
            participant: participantJid,
            peer_pn: await resolveParticipantPhone(getParticipantAltJid(msg) || participantJid)
          };

          try {
            // Buffer message for batch processing
            await messageBuffer.add(doc);
            console.log(JSON.stringify({ status: 'info', event: 'message_buffered', source: 'history', message_id: doc.message_id }));
            count++;
            backfillCounts.set(chatJid, current + 1);
            backfillTotal++;
          } catch (e) {
            console.log(JSON.stringify({ status: 'error', event: 'history_buffer_failed', message_id: id, error: e.message }));
            // Fallback to single POST
            try {
              await api.post('/whatsapp/messages', doc);
            } catch (err) {
              console.log(JSON.stringify({ status: 'error', event: 'history_http_fallback_failed', message_id: id, error: err.message }));
            }

          }
        }
        console.log(JSON.stringify({ status: 'backfill_progress', sessionId, batchInserted: count, backfillTotal }));
      } catch (e) {
        console.log(JSON.stringify({ status: 'error', error: 'Messaging-history backfill failed: ' + e.message }));
      }
    });

    sock.ev.on('messages.set', async (mset) => {
      try {
        console.log(JSON.stringify({ status: 'info', event: 'messages.set', messageCount: mset.messages.length, isLatest: mset?.isLatest }));
        const arr = Array.isArray(mset?.messages) ? mset.messages : [];
        let count = 0;
        for (const msg of arr) {
          if (!isValidMessage(msg)) continue;
          if (backfillTotal >= BACKFILL_TOTAL) { continue; }

          const fromMe = !!msg?.key?.fromMe;
          const id = (msg?.key?.id) || null;
          const messageType = msg.message ? Object.keys(msg.message)[0] : null;
          let text = (msg.message?.conversation || msg.message?.extendedTextMessage?.text || msg.message?.imageMessage?.caption || '') || '';
          if (typeof text !== 'string') text = '';
          const mediaInfo = _getMediaNodeAndType(msg);
          const hasMedia = !!mediaInfo?.node;
          const mediaNode = mediaInfo?.node || null;
          const mediaKind = mediaInfo?.type || null;

          const admin_number = getAdminNumber(msg);
          const cx_number = await resolvePeerNumber(msg);

          if (!admin_number || !cx_number || admin_number === "" || cx_number === "") {
            console.log(JSON.stringify({ status: 'warn', event: 'skip_history_missing_numbers', admin_number, cx_number, id }));
            continue;
          }

          const chatJid = msg?.key?.remoteJid || '';
          const current = backfillCounts.get(chatJid) || 0;
          if (current >= BACKFILL_PER_CHAT) { continue; }

          const participantJid = getParticipantJid(msg);
          const media = hasMedia ? await _downloadMessageMedia(sessionId, id, msg) : null;
          const r2_media_url = (hasMedia && media) ? await _uploadMediaToR2(sessionId, id, media.buffer, mediaKind, media.ext) : null;
          const doc = {
            message_id: id || `${Date.now()}_${Math.random().toString(36).slice(2)}`,
            direction: fromMe ? 'outgoing' : 'incoming',
            admin_number, cx_number, content: text, clean_content: cleanMessageContent(text),
            timestamp: new Date((msg.messageTimestamp || Math.floor(Date.now() / 1000)) * 1000),
            device: 'baileys', issent: fromMe, isread: false, message_type: messageType,
            media_mime: mediaNode?.mimetype || null,
            media_size: mediaNode?.fileLength || mediaNode?.fileLengthLow || null,
            quoted_id: msg.message?.extendedTextMessage?.contextInfo?.stanzaId || msg.message?.imageMessage?.contextInfo?.stanzaId || null,
            quoted_text: msg.message?.extendedTextMessage?.contextInfo?.quotedMessage?.conversation || null,
            remote_jid: getRemoteJid(msg),
            r2_media_url: r2_media_url || null,
            raw: msg,  // Store the complete raw message object
            participant: participantJid,
            peer_pn: await resolveParticipantPhone(getParticipantAltJid(msg) || participantJid),
          };

          try {
            // Buffer message for batch processing
            await messageBuffer.add(doc);
            console.log(JSON.stringify({ status: 'info', event: 'message_buffered', source: 'history', message_id: doc.message_id }));
            count++;
            backfillCounts.set(chatJid, current + 1);
            backfillTotal++;
          } catch (e) {
            console.log(JSON.stringify({ status: 'error', event: 'history_buffer_failed', message_id: id, error: e.message }));
            // Fallback to single POST
            try {
              await api.post('/whatsapp/messages', doc);
            } catch (err) {
              console.log(JSON.stringify({ status: 'error', event: 'history_http_fallback_failed', message_id: id, error: err.message }));
            }
          }
        }
        console.log(JSON.stringify({ status: 'backfill_progress', sessionId, batchInserted: count, backfillTotal }));
      } catch (e) {
        console.log(JSON.stringify({ status: 'error', error: 'Backfill failed: ' + e.message }));
      }
    });

    // Process incoming live messages
    sock.ev.on('messages.upsert', async (m) => {
      try {
        lastMessagesUpsertAt = nowIso();
        messageUpsertCount += Array.isArray(m.messages) ? m.messages.length : 0;

        await api.post(
          `/whatsapp/session/${sessionId}/heartbeat`,
          buildHealthPayload({ event_source: "messages.upsert" })
        ).catch(() => {});

        console.log(JSON.stringify({ status: 'info', event: 'messages.upsert', messageCount: m.messages?.length || 0 }));
        const messages = m.messages || [];
        const validMessages = [];

        for (const msg of messages) {
          if (!isValidMessage(msg)) continue;

          const msgContent = msg.message?.conversation ||
            msg.message?.extendedTextMessage?.text ||
            msg.message?.imageMessage?.caption ||
            null;

          const mediaInfo = _getMediaNodeAndType(msg);
          const hasMedia = !!mediaInfo?.node;

          const fromMe = !!msg?.key?.fromMe;
          const id = msg?.key?.id;
          const messageType = msg.message ? Object.keys(msg.message)[0] : "text";
          const mediaNode = mediaInfo?.node || null;
          const mediaKind = mediaInfo?.type || null;

          const admin_number = getAdminNumber(msg);
          const cx_number = await resolvePeerNumber(msg);
          if (!admin_number || !cx_number || admin_number === "" || cx_number === "") {
            console.log(JSON.stringify({ status: 'warn', event: 'skip_live_missing_numbers', admin_number, cx_number, id }));
            continue;
          }
          const participantJid = getParticipantJid(msg);
          const media = hasMedia ? await _downloadMessageMedia(sessionId, id, msg) : null;
          const r2_media_url = (hasMedia && media) ? await _uploadMediaToR2(sessionId, id, media.buffer, mediaKind, media.ext) : null;
          const validMessage = {
            message_id: id || `${Date.now()}_${Math.random().toString(36).slice(2)}`,
            direction: fromMe ? "outgoing" : "incoming",
            admin_number,
            cx_number,
            content: msgContent || '',
            clean_content: cleanMessageContent(msgContent || ''),
            timestamp: msg.messageTimestamp ? new Date(msg.messageTimestamp * 1000).toISOString() : new Date().toISOString(),
            message_type: messageType,
            device: "baileys",
            // from_me: fromMe,
            isread: false,
            issent: fromMe,
            remote_jid: getRemoteJid(msg),
            // participant: msg?.key?.participant,
            // message_key_id: id,
            media_mime: mediaNode?.mimetype,
            media_size: mediaNode?.fileLength || mediaNode?.fileLengthLow,
            quoted_id: msg.message?.extendedTextMessage?.contextInfo?.stanzaId || msg.message?.imageMessage?.contextInfo?.stanzaId,
            quoted_text: msg.message?.extendedTextMessage?.contextInfo?.quotedMessage?.conversation,
            r2_media_url: r2_media_url || null,
            raw: msg,  // Store the complete raw message object
            participant: participantJid,
            peer_pn: await resolveParticipantPhone(getParticipantAltJid(msg) || participantJid),
          };

          try {
            // Use message buffer for batch processing
            await messageBuffer.add(validMessage);
            lastMessageBufferedAt = nowIso();
            console.log(JSON.stringify({ status: 'info', event: 'message_buffered', source: 'live', message_id: validMessage.message_id }));
          } catch (e) {
            console.log(JSON.stringify({ status: 'error', event: 'Buffer add failed (live)', message_id: id, error: e.message }));
          }
        }
      } catch (e) {
        console.log(JSON.stringify({ status: 'error', error: 'Live message processing failed: ' + e.message }));
      }
    });

    // Handle message status updates (sent, delivered, read) 
    sock.ev.on('messages.update', async (updates) => {
      try {
        for (const { key, update } of updates) {
          if (update && update.status !== undefined) {
            const admin_number = getAdminNumber();
            if (!admin_number) continue;

            const payload = {
              message_id: key.id,
              admin_number: admin_number
            };

            // Baileys status mapping:
            // 2: STATUS_DELIVERED (equivalent to delivered)
            // 3: STATUS_READ 
            // 4: STATUS_PLAYED (voice messages)

            let shouldUpdate = false;
            if (update.status === 2) {
              payload.issent = true;
              shouldUpdate = true;
            } else if (update.status === 3 || update.status === 4) {
              payload.isread = true;
              payload.issent = true; // Optimization: if read, it must have been sent
              shouldUpdate = true;
            }

            if (shouldUpdate) {
              let retryCount = 0;
              const maxRetries = 3;
              let success = false;

              while (retryCount <= maxRetries && !success) {
                try {
                  await api.post('/whatsapp/messages', payload);
                  console.log(JSON.stringify({
                    status: 'info',
                    event: 'message_status_updated',
                    message_id: key.id,
                    whatsapp_status: update.status,
                    updates: payload
                  }));
                  success = true;
                } catch (e) {
                  retryCount++;
                  console.log(JSON.stringify({
                    status: 'warn',
                    event: 'status_update_failed',
                    message_id: key.id,
                    retry: retryCount,
                    error: e.message
                  }));
                  if (retryCount <= maxRetries) {
                    await new Promise(r => setTimeout(r, Math.random() * 5000));
                  }
                }
              }
            }
          }
        }
      } catch (e) {
        console.log(JSON.stringify({ status: 'error', error: 'Status update processing failed: ' + e.message }));
      }
    });

  };

  const shutdown = async (signal) => {
    console.log(JSON.stringify({ status: 'info', event: 'shutdown_initiated', signal }));
    try {
      if (messageBuffer) {
        await messageBuffer.stop();
        console.log(JSON.stringify({ status: 'info', event: 'buffer_flushed_on_shutdown' }));
      }

      // Notify backend if possible
      await api.post('/whatsapp/session/event', {
        session_id: sessionId,
        event: 'disconnected'
      }).catch(() => { });

      console.log(JSON.stringify({ status: 'info', event: 'disconnect_event_sent_on_shutdown' }));
    } catch (e) {
      console.log(JSON.stringify({ status: 'warn', event: 'shutdown_error', error: e.message }));
    } finally {
      cleanupBridgePortFile();
      process.exit(0);
    }
  };

  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('exit', cleanupBridgePortFile);

  startBridgeServer();
  // initial connect
  connect();

  // Safety timeout: exit with timeout JSON if no QR within 180s (do NOT kill while pairing)
  // Keep process alive; do not force-exit on a QR timeout. Frontend will handle retries.
};

main().catch(err => {
  console.log(JSON.stringify({ status: 'error', error: 'Unhandled top-level error: ' + err.message, stack: err.stack }));
  process.exit(1);
});