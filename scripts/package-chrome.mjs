#!/usr/bin/env node

import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { deflateRawSync } from 'node:zlib'

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const extensionRoot = path.join(repoRoot, 'chrome_capture', 'extension')
const outputRoot = path.join(repoRoot, 'dist', 'chrome')
const packageFiles = [
  'manifest.json',
  'icons/icon128.png',
  'src/background.js',
  'src/causality.js',
  'src/popup.html',
  'src/popup.js',
  'src/user-events.js'
]

function sha256(value) {
  return createHash('sha256').update(value).digest('hex')
}

function extensionIdFromKey(publicKey) {
  const digest = createHash('sha256')
    .update(Buffer.from(publicKey, 'base64'))
    .digest()
    .subarray(0, 16)
  return [...digest.toString('hex')]
    .map(character => String.fromCharCode('a'.charCodeAt(0) + Number.parseInt(character, 16)))
    .join('')
}

function crc32(buffer) {
  let crc = 0xffffffff
  for (const byte of buffer) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1))
    }
  }
  return (crc ^ 0xffffffff) >>> 0
}

function localHeader({ name, crc, compressedSize, size }) {
  const header = Buffer.alloc(30)
  header.writeUInt32LE(0x04034b50, 0)
  header.writeUInt16LE(20, 4)
  header.writeUInt16LE(0x0800, 6)
  header.writeUInt16LE(8, 8)
  header.writeUInt16LE(0, 10)
  header.writeUInt16LE((46 << 9) | (1 << 5) | 1, 12)
  header.writeUInt32LE(crc, 14)
  header.writeUInt32LE(compressedSize, 18)
  header.writeUInt32LE(size, 22)
  header.writeUInt16LE(Buffer.byteLength(name), 26)
  header.writeUInt16LE(0, 28)
  return header
}

function centralHeader({ name, crc, compressedSize, size, offset }) {
  const header = Buffer.alloc(46)
  header.writeUInt32LE(0x02014b50, 0)
  header.writeUInt16LE(0x0314, 4)
  header.writeUInt16LE(20, 6)
  header.writeUInt16LE(0x0800, 8)
  header.writeUInt16LE(8, 10)
  header.writeUInt16LE(0, 12)
  header.writeUInt16LE((46 << 9) | (1 << 5) | 1, 14)
  header.writeUInt32LE(crc, 16)
  header.writeUInt32LE(compressedSize, 20)
  header.writeUInt32LE(size, 24)
  header.writeUInt16LE(Buffer.byteLength(name), 28)
  header.writeUInt16LE(0, 30)
  header.writeUInt16LE(0, 32)
  header.writeUInt16LE(0, 34)
  header.writeUInt16LE(0, 36)
  header.writeUInt32LE((0o100644 << 16) >>> 0, 38)
  header.writeUInt32LE(offset, 42)
  return header
}

function endRecord({ entries, centralSize, centralOffset }) {
  const record = Buffer.alloc(22)
  record.writeUInt32LE(0x06054b50, 0)
  record.writeUInt16LE(0, 4)
  record.writeUInt16LE(0, 6)
  record.writeUInt16LE(entries, 8)
  record.writeUInt16LE(entries, 10)
  record.writeUInt32LE(centralSize, 12)
  record.writeUInt32LE(centralOffset, 16)
  record.writeUInt16LE(0, 20)
  return record
}

function deterministicZip(files) {
  const localParts = []
  const centralParts = []
  let offset = 0
  for (const file of files) {
    const name = Buffer.from(file.name, 'utf8')
    const compressed = deflateRawSync(file.contents, { level: 9 })
    const entry = {
      name: file.name,
      crc: crc32(file.contents),
      compressedSize: compressed.length,
      size: file.contents.length,
      offset
    }
    const local = localHeader(entry)
    localParts.push(local, name, compressed)
    centralParts.push(centralHeader(entry), name)
    offset += local.length + name.length + compressed.length
  }
  const central = Buffer.concat(centralParts)
  return Buffer.concat([
    ...localParts,
    central,
    endRecord({
      entries: files.length,
      centralSize: central.length,
      centralOffset: offset
    })
  ])
}

const manifest = JSON.parse(
  fs.readFileSync(path.join(extensionRoot, 'manifest.json'), 'utf8')
)
const identity = JSON.parse(
  fs.readFileSync(path.join(extensionRoot, 'store-identity.json'), 'utf8')
)
const extensionPackage = JSON.parse(
  fs.readFileSync(path.join(extensionRoot, 'package.json'), 'utf8')
)
const extensionId = extensionIdFromKey(String(manifest.key || ''))
const version = String(manifest.version || '').trim()
if (
  manifest.manifest_version !== 3 ||
  identity.schemaVersion !== 1 ||
  !['assigned', 'published'].includes(identity.status) ||
  identity.extensionId !== extensionId ||
  extensionPackage.version !== version ||
  !/^\d+(?:\.\d+){0,3}$/.test(version)
) {
  throw new Error('Chrome Web Store manifest, package version, and assigned identity do not match')
}

const files = packageFiles.map(name => {
  const target = path.join(extensionRoot, name)
  const stat = fs.lstatSync(target, { throwIfNoEntry: false })
  if (!stat?.isFile() || stat.isSymbolicLink()) {
    throw new Error(`Required extension file is missing or unsafe: ${name}`)
  }
  const contents = fs.readFileSync(target)
  if (/\.(?:html|js)$/u.test(name)) {
    const source = contents.toString('utf8')
    if (
      /\beval\s*\(/u.test(source) ||
      /\bnew\s+Function\s*\(/u.test(source) ||
      /<(?:script|link)\b[^>]+(?:src|href)=["']https?:/iu.test(source)
    ) {
      throw new Error(`Remote or dynamically evaluated code is not allowed in ${name}`)
    }
  }
  return { name, contents }
})

const archive = deterministicZip(files)
const archiveName = `hermes-chrome-provider-${version}.zip`
const archivePath = path.join(outputRoot, archiveName)
fs.mkdirSync(outputRoot, { recursive: true })
fs.writeFileSync(archivePath, archive, { mode: 0o644 })

const receipt = {
  schemaVersion: 1,
  product: 'hermes-chrome-provider',
  version,
  extensionId,
  archive: {
    path: archiveName,
    bytes: archive.length,
    sha256: sha256(archive)
  },
  files: files.map(file => ({
    path: file.name,
    bytes: file.contents.length,
    sha256: sha256(file.contents)
  }))
}
const receiptPath = path.join(outputRoot, 'package-receipt.json')
fs.writeFileSync(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, { mode: 0o644 })
console.log(JSON.stringify({ archivePath, receiptPath, ...receipt }, null, 2))
