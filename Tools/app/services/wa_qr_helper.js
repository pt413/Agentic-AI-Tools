const qrcode = require('qrcode');
const path = require('path');
const fs = require('fs');
const axios = require('axios');
const { default: makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion, DisconnectReason } = require('@whiskeysockets/baileys');
const P = require('pino');

const BACKEND_URL = process.env.BACKEND_URL || 'http://127.0.0.1:8000';

const api = axios.create({
  baseURL: `${BACKEND_URL}/connector/api`,
  withCredentials: true, // Important for CORS with credentials
  headers: {
    'Content-Type': 'application/json',
    'Accept': 'application/json'
  }
});

process.on('uncaughtException', (err) => {
  try { console.log(JSON.stringify({ status: 'error', error: `uncaughtException: ${err.message}` })); } catch (_) { }
  process.exit(1);
});
process.on('unhandledRejection', (reason) => {
  try {
    const msg = (reason && reason.message) ? reason.message : String(reason);
    console.log(JSON.stringify({ status: 'error', error: `unhandledRejection: ${msg}` }));
  } catch (_) { }
  process.exit(1);
});

const sessionId = process.argv[2];
if (!sessionId) {
  console.log(JSON.stringify({ error: 'Session ID required' }));
  process.exit(1);
}

console.log(JSON.stringify({ status: 'info', message: 'Starting WhatsApp QR helper', sessionId }));

const baseDir = __dirname;
const authFolder = path.join(baseDir, '.baileys_auth', sessionId);

try { fs.mkdirSync(authFolder, { recursive: true }); } catch (_) { }

const credsPath = path.join(authFolder, 'creds.json');
const alreadyAuth = fs.existsSync(credsPath);

console.log(JSON.stringify({ status: 'info', message: 'Auth folder ready', sessionId, alreadyAuth }));

(async () => {
  const { state, saveCreds } = await useMultiFileAuthState(authFolder);
  const { version, isLatest } = await fetchLatestBaileysVersion();
  let emitted = false;
  let qrEmitted = false;

  console.log(JSON.stringify({
    status: 'info',
    message: 'Baileys version',
    sessionId,
    version: version.join('.'),
    isLatest
  }));

  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    logger: P({ level: 'silent' }),
    browser: ['Windows', 'Chrome', '120.0.6099.109'],

    // Critical settings for modern WhatsApp
    syncFullHistory: false,
    markOnlineOnConnect: false,
    fireInitQueries: true,
    emitOwnEvents: false,

    // Timeouts
    connectTimeoutMs: 60000,
    defaultQueryTimeoutMs: 0,
    keepAliveIntervalMs: 10000,

    // Message handling
    getMessage: async () => undefined,
    shouldIgnoreJid: () => false,

    // Retries
    retryRequestDelayMs: 250,
    maxMsgRetryCount: 5,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', async (update) => {
    const { connection, qr, lastDisconnect } = update;

    console.log(JSON.stringify({
      status: 'info',
      message: 'Connection update',
      sessionId,
      connection,
      hasQR: !!qr,
      qrEmitted,
      emitted
    }));

    try {
      if (qr) {
        const qrImage = await qrcode.toDataURL(qr);

        console.log(JSON.stringify({ status: 'qr', qrCode: qrImage, sessionId }));

        // Persist QR code to disk for Python controller to pick up
        try {
          const qrPath = path.join(authFolder, 'last_qr.txt');
          fs.writeFileSync(qrPath, qrImage);
        } catch (err) {
          console.log(JSON.stringify({ status: 'info', message: 'Failed to save QR to disk', error: err.message }));
        }

        qrEmitted = true;
        return;
      }

      if (connection === 'open' && !emitted) {
        const jid = sock.user?.id;
        const phoneNumber = jid ? String(jid).split('@')[0] : null;
        const status = alreadyAuth ? 'already_authenticated' : 'ready';

        console.log(JSON.stringify({ status, sessionId, phoneNumber }));
        emitted = true;

        // NOTIFY BACKEND: Connected (ensure DB row is ready for the streamer that will follow)
        try {
          await api.post('/whatsapp/session/event', {
            session_id: sessionId,
            event: 'authenticated',
            phoneNumber: phoneNumber,
            source: 'qr_helper'
          });
          console.log(JSON.stringify({ status: 'info', message: 'Notified backend of successful auth', sessionId }));
        } catch (e) {
          console.log(JSON.stringify({ status: 'warn', message: 'Failed to notify backend of auth', error: e.message }));
        }

        setTimeout(() => process.exit(0), 2000);
        return;
      }

      if (connection === 'close') {
        const statusCode = lastDisconnect?.error?.output?.statusCode;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

        console.log(JSON.stringify({
          status: 'info',
          message: 'Connection closed',
          sessionId,
          statusCode,
          shouldReconnect
        }));

        if (qrEmitted && !emitted && shouldReconnect) {
          console.log(JSON.stringify({ status: 'info', message: 'Waiting for scan', sessionId }));
          return;
        }

        if (!shouldReconnect || emitted) {
          process.exit(emitted ? 0 : 1);
        }
      }
    } catch (e) {
      console.log(JSON.stringify({ status: 'error', error: e.message, sessionId }));
      if (!qrEmitted && !emitted) {
        process.exit(1);
      }
    }
  });

  setTimeout(() => {
    if (!emitted) {
      console.log(JSON.stringify({ status: 'timeout', sessionId }));
      process.exit(0);
    }
  }, 300000);
})();
