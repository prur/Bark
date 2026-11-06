const {test} = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {encrypt} = require('./zbark_encrypt.cjs');

test('GCM uses full independent nonces and authenticates the entire payload', () => {
  const key = crypto.randomBytes(32);
  const fields = {algorithm: 'AES256', mode: 'GCM', padding: 'noPadding', key: key.toString('base64'), keyEncoding: 'base64'};
  const payload = {id: 'fixture', title: 'secret-title', body: 'secret-body', isArchive: '1'};
  const first = encrypt(payload, fields), second = encrypt(payload, fields);
  assert.notEqual(first.iv, second.iv);
  assert.equal(Buffer.from(first.iv, 'base64').length, 12);
  assert.equal(first.iv_encoding, 'base64');
  const data = Buffer.from(first.ciphertext, 'base64');
  const decrypt = crypto.createDecipheriv('aes-256-gcm', key, Buffer.from(first.iv, 'base64'));
  decrypt.setAuthTag(data.subarray(-16));
  assert.deepEqual(JSON.parse(Buffer.concat([decrypt.update(data.subarray(0, -16)), decrypt.final()])), payload);
  assert(!JSON.stringify(first).includes('secret-body'));
  assert(!JSON.stringify(first).includes(fields.key));
  assert.throws(() => encrypt(payload, {...fields, key: 'bad-base64!'}));
  assert.throws(() => encrypt(payload, {...fields, mode: 'CBC'}));
});
