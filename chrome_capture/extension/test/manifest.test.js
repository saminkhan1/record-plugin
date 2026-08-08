import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync
} from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { validateChromeExtensionIdentity } from '../../../scripts/release-contract.mjs'

const extensionRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const iconPath = path.join(extensionRoot, 'icons', 'icon128.png')

test('Web Store manifest uses the reviewed 128px app icon', () => {
  const manifest = JSON.parse(readFileSync(path.join(extensionRoot, 'manifest.json'), 'utf8'))
  const extensionPackage = JSON.parse(
    readFileSync(path.join(extensionRoot, 'package.json'), 'utf8')
  )
  const extensionLock = JSON.parse(
    readFileSync(path.join(extensionRoot, 'package-lock.json'), 'utf8')
  )
  const identity = JSON.parse(readFileSync(path.join(extensionRoot, 'store-identity.json'), 'utf8'))
  const icon = readFileSync(iconPath)
  const developmentExtensionId = [
    ...crypto.createHash('sha256').update(Buffer.from(manifest.key, 'base64')).digest().subarray(0, 16).toString('hex')
  ]
    .map(character => String.fromCharCode('a'.charCodeAt(0) + Number.parseInt(character, 16)))
    .join('')

  assert.equal(manifest.name, 'Record Chrome Provider')
  assert.equal(manifest.version, '0.2.3')
  assert.equal(extensionPackage.version, manifest.version)
  assert.equal(extensionLock.version, manifest.version)
  assert.equal(extensionLock.packages?.['']?.version, manifest.version)
  assert.equal(
    manifest.description,
    'Capture active Chrome workflow actions into a local Record workflow for review.'
  )
  assert.equal(identity.schemaVersion, 2)
  assert.ok(['assigned', 'published'].includes(identity.status))
  assert.equal(developmentExtensionId, identity.extensionId)
  assert.equal(manifest.icons?.['128'], 'icons/icon128.png')
  assert.equal(manifest.action?.default_icon?.['128'], 'icons/icon128.png')
  assert.deepEqual(
    manifest.content_scripts?.[0]?.js,
    ['src/url-sanitizer.js', 'src/causality.js', 'src/user-events.js']
  )
  assert.equal(icon.readUInt32BE(16), 128)
  assert.equal(icon.readUInt32BE(20), 128)
  assert.equal(
    crypto.createHash('sha256').update(icon).digest('hex'),
    'c82640fef1fc216379ee83cba97112ba667c0c334596b87dfa246c4bab7c19fb'
  )
})

test('Store identity distinguishes the published item from the unpublished candidate bytes', () => {
  const repositoryRoot = path.resolve(extensionRoot, '..', '..')
  const chrome = validateChromeExtensionIdentity(repositoryRoot)

  assert.equal(chrome.status, 'published')
  assert.equal(chrome.version, '0.2.3')
  assert.equal(chrome.publishedVersion, '0.2.2')
  assert.equal(chrome.currentVersionPublished, false)
  assert.throws(
    () => validateChromeExtensionIdentity(repositoryRoot, { requirePublishedVersion: true }),
    /Chrome extension 0\.2\.3 is not published/u
  )
})

test('release identity validation rejects a stale extension lock version', () => {
  const repositoryRoot = mkdtempSync(path.join(tmpdir(), 'record-chrome-version-contract-'))
  const copiedExtensionRoot = path.join(repositoryRoot, 'chrome_capture', 'extension')
  mkdirSync(copiedExtensionRoot, { recursive: true })
  try {
    for (const name of ['manifest.json', 'package.json', 'package-lock.json', 'store-identity.json']) {
      copyFileSync(path.join(extensionRoot, name), path.join(copiedExtensionRoot, name))
    }
    const lockPath = path.join(copiedExtensionRoot, 'package-lock.json')
    const lock = JSON.parse(readFileSync(lockPath, 'utf8'))
    lock.packages[''].version = '0.2.2'
    writeFileSync(lockPath, `${JSON.stringify(lock, null, 2)}\n`)

    assert.throws(
      () => validateChromeExtensionIdentity(repositoryRoot),
      /manifest, package version, identity, and published provenance are invalid/u
    )
  } finally {
    rmSync(repositoryRoot, { recursive: true, force: true })
  }
})
