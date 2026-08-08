#!/usr/bin/env node

import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const VERSION_PATTERN = /^\d+\.\d+\.\d+$/
const CHROME_VERSION_PATTERN = /^\d+(?:\.\d+){0,3}$/
const COMMIT_PATTERN = /^[0-9a-f]{40}$/
const SHA256_PATTERN = /^[0-9a-f]{64}$/

function readText(target) {
  try {
    return fs.readFileSync(target, 'utf8')
  } catch (error) {
    throw new Error(`Release version source is unreadable: ${target}: ${error.message}`)
  }
}

function readJson(target) {
  try {
    const value = JSON.parse(readText(target))
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('not an object')
    return value
  } catch (error) {
    throw new Error(`Release version JSON is invalid: ${target}: ${error.message}`)
  }
}

function yamlVersion(target) {
  const matches = [...readText(target).matchAll(/^\s*version:\s*["']?([^"'#\s]+)["']?\s*$/gmu)]
  if (matches.length !== 1) throw new Error(`Record plugin manifest must declare exactly one version: ${target}`)
  return matches[0][1]
}

function plistVersion(target, key) {
  const match = readText(target).match(
    new RegExp(`<key>\\s*${key}\\s*</key>\\s*<string>([^<]+)</string>`, 'u')
  )
  if (!match) throw new Error(`Record service Info.plist is missing ${key}: ${target}`)
  return match[1].trim()
}

function recorderProtocolVersion(target) {
  const match = readText(target).match(
    /"serverInfo"\s*:\s*\[\s*"name"\s*:\s*"hermes-recorder"\s*,\s*"version"\s*:\s*"([^"]+)"\s*\]/u
  )
  if (!match) throw new Error(`Recorder MCP serverInfo version is missing: ${target}`)
  return match[1]
}

export function validateProductVersions(repoRoot) {
  repoRoot = path.resolve(repoRoot)
  const version = String(readJson(path.join(repoRoot, 'package.json')).version || '').trim()
  if (!VERSION_PATTERN.test(version)) throw new Error(`Record package version must be semantic x.y.z, got ${JSON.stringify(version)}`)
  const plist = path.join(repoRoot, 'native', 'recorder-service', 'Resources', 'Info.plist')
  const components = {
    backendPlugin: yamlVersion(path.join(repoRoot, 'plugin.yaml')),
    dashboard: String(readJson(path.join(repoRoot, 'dashboard', 'manifest.json')).version || '').trim(),
    desktopPlugin: String(readJson(path.join(repoRoot, 'desktop-plugin', 'package.json')).version || '').trim(),
    recorderBundleShort: plistVersion(plist, 'CFBundleShortVersionString'),
    recorderBundleBuild: plistVersion(plist, 'CFBundleVersion'),
    recorderProtocol: recorderProtocolVersion(path.join(repoRoot, 'native', 'recorder-service', 'Sources', 'HermesRecorderClient', 'main.swift'))
  }
  const mismatches = Object.entries(components)
    .filter(([, componentVersion]) => componentVersion !== version)
    .map(([name, componentVersion]) => `${name}=${JSON.stringify(componentVersion)}`)
  if (mismatches.length) throw new Error(`Record product versions must all equal ${version}: ${mismatches.join(', ')}`)
  return { version, components }
}

export function chromeExtensionIdFromKey(publicKey) {
  const digest = createHash('sha256')
    .update(Buffer.from(publicKey, 'base64'))
    .digest()
    .subarray(0, 16)
  return [...digest.toString('hex')]
    .map(character => String.fromCharCode('a'.charCodeAt(0) + Number.parseInt(character, 16)))
    .join('')
}

export function validateChromeExtensionIdentity(repoRoot, { requirePublishedVersion = false } = {}) {
  repoRoot = path.resolve(repoRoot)
  const extensionRoot = path.join(repoRoot, 'chrome_capture', 'extension')
  const manifest = readJson(path.join(extensionRoot, 'manifest.json'))
  const extensionPackage = readJson(path.join(extensionRoot, 'package.json'))
  const extensionLock = readJson(path.join(extensionRoot, 'package-lock.json'))
  const identity = readJson(path.join(extensionRoot, 'store-identity.json'))
  const version = String(manifest.version || '').trim()
  const extensionId = chromeExtensionIdFromKey(String(manifest.key || ''))
  const publishedVersion = String(identity.publishedVersion || '').trim()
  const publishedArchiveSha256 = String(identity.publishedArchiveSha256 || '').trim()
  const publishedSourceCommit = String(identity.publishedSourceCommit || '').trim()
  if (
    manifest.manifest_version !== 3 ||
    !CHROME_VERSION_PATTERN.test(version) ||
    extensionPackage.version !== version ||
    extensionLock.version !== version ||
    extensionLock.packages?.['']?.version !== version ||
    identity.schemaVersion !== 2 ||
    !['assigned', 'published'].includes(identity.status) ||
    identity.extensionId !== extensionId ||
    (identity.status === 'published' && (
      !CHROME_VERSION_PATTERN.test(publishedVersion) ||
      !SHA256_PATTERN.test(publishedArchiveSha256) ||
      !COMMIT_PATTERN.test(publishedSourceCommit)
    ))
  ) {
    throw new Error('Chrome Web Store manifest, package version, identity, and published provenance are invalid')
  }
  const currentVersionPublished =
    identity.status === 'published' && publishedVersion === version
  if (requirePublishedVersion && !currentVersionPublished) {
    throw new Error(
      `Chrome extension ${version} is not published: approved Store version is ${publishedVersion || 'none'}`
    )
  }
  return {
    version,
    extensionId,
    status: identity.status,
    publishedVersion,
    publishedArchiveSha256,
    publishedSourceCommit,
    currentVersionPublished
  }
}

function git(repoRoot, args, { preserveLeadingWhitespace = false } = {}) {
  try {
    const output = execFileSync('/usr/bin/git', ['-C', repoRoot, ...args], {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe']
    })
    return preserveLeadingWhitespace ? output.trimEnd() : output.trim()
  } catch (error) {
    const detail = String(error.stderr || error.stdout || error.message).trim()
    throw new Error(`Git release check failed (${args.join(' ')}): ${detail}`)
  }
}

function requireCleanSource(repoRoot) {
  repoRoot = path.resolve(repoRoot)
  if (path.resolve(git(repoRoot, ['rev-parse', '--show-toplevel'])) !== repoRoot) {
    throw new Error(`Release must run at its Git root: ${repoRoot}`)
  }
  const status = git(repoRoot, ['status', '--porcelain=v1', '--untracked-files=all'], {
    preserveLeadingWhitespace: true
  })
  if (status) {
    throw new Error(
      `Release source must be clean; changed paths: ${status.split('\n').map(line => line.slice(3)).join(', ')}`
    )
  }
  const commit = git(repoRoot, ['rev-parse', 'HEAD'])
  if (!COMMIT_PATTERN.test(commit)) throw new Error(`Release HEAD is invalid: ${commit}`)
  return { commit }
}

export function requireChromeReleaseSource(repoRoot, expectedVersion) {
  const chrome = validateChromeExtensionIdentity(repoRoot)
  if (expectedVersion && chrome.version !== expectedVersion) {
    throw new Error(
      `Chrome release source version changed from ${expectedVersion} to ${chrome.version}`
    )
  }
  return requireCleanSource(repoRoot)
}

export function requirePublishedChromeSource(repoRoot, chrome, packageFiles) {
  if (!chrome.currentVersionPublished) {
    throw new Error(
      `Chrome extension ${chrome.version} is not published: approved Store version is ${chrome.publishedVersion || 'none'}`
    )
  }
  const resolvedCommit = git(repoRoot, [
    'rev-parse', '--verify', `${chrome.publishedSourceCommit}^{commit}`
  ])
  if (resolvedCommit !== chrome.publishedSourceCommit) {
    throw new Error('Published Chrome source commit is not an exact commit object')
  }
  if (git(repoRoot, ['merge-base', chrome.publishedSourceCommit, 'HEAD']) !== chrome.publishedSourceCommit) {
    throw new Error('Published Chrome source commit is not an ancestor of the release source')
  }
  const changed = git(repoRoot, [
    'diff', '--name-only', chrome.publishedSourceCommit, '--', ...packageFiles.map(
      relativePath => path.posix.join('chrome_capture/extension', relativePath)
    )
  ])
  if (changed) {
    throw new Error(`Published Chrome extension files changed after approval: ${changed.split('\n').join(', ')}`)
  }
  return { commit: chrome.publishedSourceCommit }
}

export function requireReleaseSource(repoRoot, expectedVersion) {
  repoRoot = path.resolve(repoRoot)
  const versions = validateProductVersions(repoRoot)
  if (expectedVersion && versions.version !== expectedVersion) throw new Error(`Release source version changed from ${expectedVersion} to ${versions.version}`)
  const { commit } = requireCleanSource(repoRoot)
  const tag = `v${versions.version}`
  let type
  try { type = git(repoRoot, ['cat-file', '-t', `refs/tags/${tag}`]) } catch { throw new Error(`Record release requires annotated tag ${tag} at HEAD`) }
  if (type !== 'tag') throw new Error(`Record release tag ${tag} must be annotated`)
  const tagCommit = git(repoRoot, ['rev-parse', '--verify', `refs/tags/${tag}^{commit}`])
  if (tagCommit !== commit) throw new Error(`Record release tag ${tag} does not point to HEAD ${commit}`)
  return { commit, tag }
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
if (isMain) {
  const args = process.argv.slice(2)
  const unknown = args.filter(
    value => !['--release-source', '--chrome-published'].includes(value)
  )
  if (unknown.length) throw new Error(`Unknown argument(s): ${unknown.join(' ')}`)
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
  const versions = validateProductVersions(repoRoot)
  const chrome = validateChromeExtensionIdentity(repoRoot, {
    requirePublishedVersion: args.includes('--chrome-published')
  })
  const source = args.includes('--release-source') ? requireReleaseSource(repoRoot, versions.version) : null
  console.log(JSON.stringify({ ...versions, chrome, source }, null, 2))
}
