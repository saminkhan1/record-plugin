import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const extensionRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const iconPath = path.join(extensionRoot, 'icons', 'icon128.png')

test('Web Store manifest uses the reviewed 128px app icon', () => {
  const manifest = JSON.parse(readFileSync(path.join(extensionRoot, 'manifest.json'), 'utf8'))
  const identity = JSON.parse(readFileSync(path.join(extensionRoot, 'store-identity.json'), 'utf8'))
  const icon = readFileSync(iconPath)
  const developmentExtensionId = [
    ...crypto.createHash('sha256').update(Buffer.from(manifest.key, 'base64')).digest().subarray(0, 16).toString('hex')
  ]
    .map(character => String.fromCharCode('a'.charCodeAt(0) + Number.parseInt(character, 16)))
    .join('')

  assert.equal(manifest.name, 'Record Chrome Provider BETA')
  assert.match(manifest.description, /^THIS EXTENSION IS FOR BETA TESTING\./)
  assert.ok(['bootstrap-required', 'assigned'].includes(identity.status))
  if (identity.status === 'bootstrap-required') {
    assert.equal(identity.extensionId, '')
    assert.equal(developmentExtensionId, identity.provisionalDevelopmentExtensionId)
  } else {
    assert.equal(developmentExtensionId, identity.extensionId)
  }
  assert.equal(manifest.icons?.['128'], 'icons/icon128.png')
  assert.equal(manifest.action?.default_icon?.['128'], 'icons/icon128.png')
  assert.equal(icon.readUInt32BE(16), 128)
  assert.equal(icon.readUInt32BE(20), 128)
  assert.equal(
    crypto.createHash('sha256').update(icon).digest('hex'),
    'd9a0e51189953769dab6add58c8f55dcb0231e8430affafdd9045b7848631fec'
  )
})
