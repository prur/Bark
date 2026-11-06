// AES-GCM envelope compatible with the Z Bark notification extension.
// Private configuration arrives on stdin; no key appears in argv or output.
const crypto = require('node:crypto');

function encrypt(payload, fields) {
  if (fields.algorithm !== 'AES256' || fields.mode !== 'GCM' || fields.padding !== 'noPadding') {
    throw new Error('Sender requires the recommended AES-256-GCM configuration');
  }
  if (typeof fields.key !== 'string' || !['base64', undefined].includes(fields.keyEncoding)) {
    throw new Error('Unsupported key encoding');
  }
  const key = Buffer.from(fields.key, fields.keyEncoding || 'utf8');
  if (key.length !== 32 || (fields.keyEncoding === 'base64' && key.toString('base64') !== fields.key)) {
    throw new Error('Invalid AES-256 key');
  }
  if (!payload || typeof payload.id !== 'string' || !payload.id) throw new Error('A unique id is required');
  const iv = crypto.randomBytes(12); // Fresh full 96-bit nonce on every call.
  const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
  const encrypted = Buffer.concat([cipher.update(JSON.stringify(payload), 'utf8'), cipher.final()]);
  return {id: payload.id, ciphertext: Buffer.concat([encrypted, cipher.getAuthTag()]).toString('base64'),
          iv: iv.toString('base64'), iv_encoding: 'base64'};
}

module.exports = {encrypt};
if (require.main === module) {
  let input = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', chunk => { input += chunk; });
  process.stdin.on('end', () => {
    try { const {payload, fields} = JSON.parse(input); console.log(JSON.stringify(encrypt(payload, fields))); }
    catch { console.error('Encryption failed; check the protected configuration'); process.exitCode = 1; }
  });
}
