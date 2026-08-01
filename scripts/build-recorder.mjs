#!/usr/bin/env node

import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const sourceRoot = path.join(repoRoot, 'native', 'recorder-service')
const defaultOutput = path.join(repoRoot, 'dist', 'recorder-macos-arm64')
const clientIdentifier = 'com.hermes.recorder.client'
const serviceIdentifier = 'com.hermes.recorder.service'
const installerIdentifier = 'com.hermes.record.installer'
const packageJson = JSON.parse(fs.readFileSync(path.join(repoRoot, 'package.json'), 'utf8'))

function argument(args, name, fallback = '') {
  const index = args.indexOf(name)
  if (index < 0) return fallback
  if (index + 1 >= args.length) throw new Error(`${name} requires a value`)
  const value = args[index + 1]
  args.splice(index, 2)
  return value
}

function run(command, args, { capture = false, cwd } = {}) {
  return execFileSync(command, args, {
    cwd,
    encoding: capture ? 'utf8' : undefined,
    stdio: capture ? ['ignore', 'pipe', 'pipe'] : 'inherit'
  })
}

function signingIdentities() {
  const output = run(
    '/usr/bin/security',
    ['find-identity', '-v', '-p', 'codesigning'],
    { capture: true }
  )
  return [...output.matchAll(/^\s*\d+\)\s+[0-9A-F]+\s+"(.+)"$/gm)]
    .map(match => match[1])
}

function signingIdentity({ release }) {
  const configured = String(process.env.HERMES_RECORDER_SIGNING_IDENTITY || '').trim()
  const allowAdhocDevelopment =
    !release && process.env.HERMES_RECORDER_ALLOW_ADHOC_DEV === '1'
  if (!configured && allowAdhocDevelopment) return '-'
  const identities = signingIdentities()
  let identity = configured

  if (!identity) {
    const developerIds = identities.filter(value => value.startsWith('Developer ID Application:'))
    if (developerIds.length === 1) {
      identity = developerIds[0]
    } else if (!release && identities.length === 1) {
      identity = identities[0]
    }
  }

  if (!identity || !identities.includes(identity)) {
    throw new Error(
      'A stable macOS code-signing identity is required. Set ' +
      'HERMES_RECORDER_SIGNING_IDENTITY to one of `security find-identity -v -p codesigning`.'
    )
  }
  if (release && !identity.startsWith('Developer ID Application:')) {
    throw new Error('HERMES_RELEASE_BUILD=1 requires a Developer ID Application identity')
  }

  const expectedTeamId = String(process.env.APPLE_TEAM_ID || '').trim()
  if (expectedTeamId && !identity.endsWith(`(${expectedTeamId})`)) {
    throw new Error(`Recorder signing identity does not match APPLE_TEAM_ID=${expectedTeamId}`)
  }
  return identity
}

function sign(target, { identity, identifier, release }) {
  const args = ['--force']
  if (release) args.push('--options', 'runtime', '--timestamp')
  args.push('--identifier', identifier, '--sign', identity, target)
  run('/usr/bin/codesign', args)
}

function requireArm64(target) {
  const architectures = run('/usr/bin/lipo', ['-archs', target], { capture: true }).trim()
  if (architectures !== 'arm64') {
    throw new Error(`Recorder payload must be arm64-only, got "${architectures}" for ${target}`)
  }
}

function sha256(target) {
  return createHash('sha256').update(fs.readFileSync(target)).digest('hex')
}

const args = process.argv.slice(2)
const finalOutputRoot = path.resolve(argument(args, '--output', defaultOutput))
const outputRoot = path.join(
  path.dirname(finalOutputRoot),
  `.${path.basename(finalOutputRoot)}.build-${process.pid}-${Date.now()}`
)
const configuration = argument(
  args,
  '--configuration',
  process.env.HERMES_RECORDER_SWIFT_CONFIGURATION || 'release'
)
if (args.length) throw new Error(`Unknown argument(s): ${args.join(' ')}`)
process.on('exit', () => {
  fs.rmSync(outputRoot, { recursive: true, force: true })
})
if (process.platform !== 'darwin' || process.arch !== 'arm64') {
  throw new Error('Record recorder packaging supports Apple-silicon macOS only')
}
if (!fs.statSync(path.join(sourceRoot, 'Package.swift'), { throwIfNoEntry: false })?.isFile()) {
  throw new Error(`Recorder source is incomplete: ${sourceRoot}`)
}

const release = process.env.HERMES_RELEASE_BUILD === '1'
const identity = signingIdentity({ release })
const teamId = identity.match(/\(([A-Z0-9]+)\)$/)?.[1] || ''
if (release && !teamId) {
  throw new Error('Release signing identity is missing its Apple Team ID')
}
run(
  '/usr/bin/swift',
  ['build', '--package-path', sourceRoot, '--configuration', configuration, '--arch', 'arm64']
)
const binaryRoot = run(
  '/usr/bin/swift',
  [
    'build',
    '--package-path',
    sourceRoot,
    '--configuration',
    configuration,
    '--arch',
    'arm64',
    '--show-bin-path'
  ],
  { capture: true }
).trim()

const builtClient = path.join(binaryRoot, 'hermes-recorder')
const builtService = path.join(binaryRoot, 'hermes-recorder-service')
const builtInstaller = path.join(binaryRoot, 'record-installer')
for (const target of [builtClient, builtService, builtInstaller]) {
  if (!fs.statSync(target, { throwIfNoEntry: false })?.isFile()) {
    throw new Error(`Swift did not produce ${target}`)
  }
}

fs.rmSync(outputRoot, { recursive: true, force: true })
const serviceApp = path.join(outputRoot, 'Hermes Recorder Service.app')
const appContents = path.join(serviceApp, 'Contents')
const stagedClient = path.join(outputRoot, 'hermes-recorder')
const stagedService = path.join(appContents, 'MacOS', 'hermes-recorder-service')
const stagedInstaller = path.join(outputRoot, 'record-installer')
fs.mkdirSync(path.dirname(stagedService), { recursive: true })
fs.copyFileSync(builtClient, stagedClient)
fs.copyFileSync(builtService, stagedService)
fs.copyFileSync(builtInstaller, stagedInstaller)
fs.copyFileSync(
  path.join(sourceRoot, 'Resources', 'Info.plist'),
  path.join(appContents, 'Info.plist')
)
fs.chmodSync(stagedClient, 0o755)
fs.chmodSync(stagedService, 0o755)
fs.chmodSync(stagedInstaller, 0o755)

sign(stagedClient, { identity, identifier: clientIdentifier, release })
sign(stagedService, { identity, identifier: serviceIdentifier, release })
sign(serviceApp, { identity, identifier: serviceIdentifier, release })
sign(stagedInstaller, { identity, identifier: installerIdentifier, release })

for (const target of [stagedClient, stagedService, stagedInstaller]) requireArm64(target)
run('/usr/bin/codesign', ['--verify', '--strict', '--verbose=4', stagedClient])
run('/usr/bin/codesign', ['--verify', '--deep', '--strict', '--verbose=4', serviceApp])
run('/usr/bin/codesign', ['--verify', '--strict', '--verbose=4', stagedInstaller])

const receipt = {
  schemaVersion: 2,
  product: 'record',
  version: String(packageJson.version),
  platform: 'darwin',
  arch: 'arm64',
  configuration,
  release,
  signingIdentity: identity === '-' ? 'adhoc' : identity,
  teamId,
  identifiers: {
    client: clientIdentifier,
    service: serviceIdentifier
  },
  files: {
    client: {
      path: 'hermes-recorder',
      sha256: sha256(stagedClient)
    },
    service: {
      path: 'Hermes Recorder Service.app/Contents/MacOS/hermes-recorder-service',
      sha256: sha256(stagedService)
    }
  },
  installerLauncher: {
    identifier: installerIdentifier,
    path: 'record-installer',
    sha256: sha256(stagedInstaller)
  }
}
fs.writeFileSync(
  path.join(outputRoot, 'recorder-manifest.json'),
  `${JSON.stringify(receipt, null, 2)}\n`,
  { mode: 0o600 }
)

const backupRoot = path.join(
  path.dirname(finalOutputRoot),
  `.${path.basename(finalOutputRoot)}.backup-${process.pid}-${Date.now()}`
)
let hasBackup = false
try {
  if (fs.existsSync(finalOutputRoot)) {
    fs.renameSync(finalOutputRoot, backupRoot)
    hasBackup = true
  }
  fs.renameSync(outputRoot, finalOutputRoot)
} catch (error) {
  if (!fs.existsSync(finalOutputRoot) && hasBackup) {
    fs.renameSync(backupRoot, finalOutputRoot)
  }
  throw error
}
if (hasBackup) {
  try {
    fs.rmSync(backupRoot, { recursive: true, force: true })
  } catch (error) {
    console.warn(
      `Build completed but could not remove the previous output backup ${backupRoot}: ${error.message}`
    )
  }
}

console.log(JSON.stringify({ outputRoot: finalOutputRoot, ...receipt }, null, 2))
