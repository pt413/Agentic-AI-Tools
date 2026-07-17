const fs = require('fs');
const path = require('path');
const http = require('http');
const https = require('https');
const { S3Client, PutObjectCommand } = require('@aws-sdk/client-s3');

function _isHttpUrl(s) {
  if (!s || typeof s !== 'string') return false;
  return s.startsWith('http://') || s.startsWith('https://');
}

function _guessContentTypeFromExt(ext) {
  const e = (ext || '').toLowerCase();
  if (e === '.jpg' || e === '.jpeg') return 'image/jpeg';
  if (e === '.png') return 'image/png';
  if (e === '.webp') return 'image/webp';
  if (e === '.gif') return 'image/gif';
  if (e === '.mp4') return 'video/mp4';
  if (e === '.mp3') return 'audio/mpeg';
  if (e === '.ogg') return 'audio/ogg';
  if (e === '.pdf') return 'application/pdf';
  return 'application/octet-stream';
}

function _normalizeExt(ext) {
  if (!ext) return '';
  return ext.startsWith('.') ? ext : `.${ext}`;
}

function _randomId() {
  return `${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

function _resolveR2Config(overrides = {}) {
  const cfg = {
    endpoint: overrides.endpoint || process.env.R2_ENDPOINT || (process.env.R2_ACCOUNT_ID ? `https://${process.env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com` : undefined),
    region: overrides.region || process.env.R2_REGION || 'auto',
    accessKeyId: overrides.accessKeyId || process.env.R2_ACCESS_KEY_ID,
    secretAccessKey: overrides.secretAccessKey || process.env.R2_SECRET_ACCESS_KEY,
    bucket: overrides.bucket || process.env.R2_BUCKET,
    publicBaseUrl: overrides.publicBaseUrl || process.env.R2_PUBLIC_BASE_URL,
  };

  const missing = [];
  if (!cfg.endpoint) missing.push('R2_ENDPOINT or R2_ACCOUNT_ID');
  if (!cfg.accessKeyId) missing.push('R2_ACCESS_KEY_ID');
  if (!cfg.secretAccessKey) missing.push('R2_SECRET_ACCESS_KEY');
  if (!cfg.bucket) missing.push('R2_BUCKET');
  if (missing.length) {
    throw new Error(`Missing R2 configuration: ${missing.join(', ')}`);
  }

  return cfg;
}

function _getS3Client(cfg) {
  return new S3Client({
    region: cfg.region,
    endpoint: cfg.endpoint,
    credentials: {
      accessKeyId: cfg.accessKeyId,
      secretAccessKey: cfg.secretAccessKey,
    },
  });
}

function _downloadToBuffer(url) {
  return new Promise((resolve, reject) => {
    const lib = url.startsWith('https://') ? https : http;

    const req = lib.get(url, (res) => {
      const status = res.statusCode || 0;
      if (status >= 300 && status < 400 && res.headers.location) {
        const redirected = res.headers.location.startsWith('http') ? res.headers.location : new URL(res.headers.location, url).toString();
        res.resume();
        _downloadToBuffer(redirected).then(resolve).catch(reject);
        return;
      }

      if (status < 200 || status >= 300) {
        res.resume();
        reject(new Error(`Failed to fetch ${url}: HTTP ${status}`));
        return;
      }

      const chunks = [];
      res.on('data', (d) => chunks.push(d));
      res.on('end', () => {
        resolve({
          buffer: Buffer.concat(chunks),
          contentType: res.headers['content-type'],
        });
      });
    });

    req.on('error', reject);
  });
}

async function uploadToR2({
  sourceUrlOrPath,
  keyPrefix = 'uploads',
  key,
  contentType,
  r2 = {},
} = {}) {
  if (!sourceUrlOrPath) {
    throw new Error('uploadToR2: sourceUrlOrPath is required');
  }

  const cfg = _resolveR2Config(r2);
  const client = _getS3Client(cfg);

  let body;
  let inferredContentType = contentType;
  let ext = '';

  if (Buffer.isBuffer(sourceUrlOrPath)) {
    body = sourceUrlOrPath;
    inferredContentType = inferredContentType || _guessContentTypeFromExt(key ? path.extname(key) : '');
  } else if (_isHttpUrl(sourceUrlOrPath)) {
    const urlObj = new URL(sourceUrlOrPath);
    ext = path.extname(urlObj.pathname || '');
    const dl = await _downloadToBuffer(sourceUrlOrPath);
    body = dl.buffer;
    inferredContentType = inferredContentType || dl.contentType || _guessContentTypeFromExt(ext);
  } else {
    const filePath = sourceUrlOrPath;
    ext = path.extname(filePath);
    body = fs.readFileSync(filePath);
    inferredContentType = inferredContentType || _guessContentTypeFromExt(ext);
  }

  const safePrefix = String(keyPrefix || 'uploads').replace(/^\/+|\/+$/g, '');
  const objKey = key || `${safePrefix}/${new Date().toISOString().slice(0, 10)}/${_randomId()}${_normalizeExt(ext)}`;

  await client.send(
    new PutObjectCommand({
      Bucket: cfg.bucket,
      Key: objKey,
      Body: body,
      ContentType: inferredContentType,
    })
  );

  const publicUrl = cfg.publicBaseUrl
    ? `${cfg.publicBaseUrl.replace(/\/$/, '')}/${objKey}`
    : undefined;

  return { key: objKey, url: publicUrl, bucket: cfg.bucket, endpoint: cfg.endpoint };
}

module.exports = {
  uploadToR2,
};
