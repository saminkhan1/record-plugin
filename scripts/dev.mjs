#!/usr/bin/env node

import { spawn, spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { clean, cleanCaches } from './clean.mjs'
import {
  validateChromeExtensionIdentity,
  validateProductVersions
} from './release-contract.mjs'
import { runtimeFiles, validateRuntimeFiles } from './runtime-files.mjs'

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const defaultHermesHome = path.join(os.homedir(), '.hermes')
const hermesHome = path.resolve(process.env.HERMES_HOME || defaultHermesHome)
const hostRoot = path.resolve(
  process.env.HERMES_SOURCE_ROOT || path.join(repoRoot, '..', 'hermes-agent')
)
const hermesPython = path.resolve(
  process.env.HERMES_PYTHON || path.join(hostRoot, '.venv', 'bin', 'python')
)
const hermesBin = path.resolve(
  process.env.HERMES_BIN || path.join(hostRoot, '.venv', 'bin', 'hermes')
)
const productVersions = validateProductVersions(repoRoot)
const chromeIdentity = validateChromeExtensionIdentity(repoRoot)
const buildOnly = process.argv.slice(2).includes('--build-only')
const unknownArguments = process.argv
  .slice(2)
  .filter(value => value !== '--build-only')

function fail(message) {
  throw new Error(message)
}

function requireFile(target, label) {
  if (!fs.statSync(target, { throwIfNoEntry: false })?.isFile()) {
    fail(`${label} is missing: ${target}`)
  }
}

function run(command, args, { cwd = repoRoot, env = process.env, allowFailure = false } = {}) {
  const result = spawnSync(command, args, {
    cwd,
    env,
    stdio: 'inherit',
    shell: false
  })
  if (result.error) throw result.error
  if (!allowFailure && result.status !== 0) {
    fail(`${command} ${args.join(' ')} failed with exit code ${result.status}`)
  }
  return result.status ?? 1
}

function sha256(target) {
  return createHash('sha256').update(fs.readFileSync(target)).digest('hex')
}

function copyFile(source, destination) {
  requireFile(source, 'Build input')
  fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 })
  fs.copyFileSync(source, destination)
  fs.chmodSync(destination, fs.statSync(source).mode & 0o777)
}

function stageDirectory(target, populate) {
  fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 })
  const temporary = path.join(
    path.dirname(target),
    `.${path.basename(target)}.dev-${process.pid}-${Date.now()}`
  )
  fs.rmSync(temporary, { recursive: true, force: true })
  fs.mkdirSync(temporary, { recursive: true, mode: 0o700 })
  try {
    populate(temporary)
    return temporary
  } catch (error) {
    fs.rmSync(temporary, { recursive: true, force: true })
    throw error
  }
}

function assertReplaceable(target, kind) {
  const targetState = fs.lstatSync(target, { throwIfNoEntry: false })
  if (!targetState) return
  if (!targetState.isDirectory() || targetState.isSymbolicLink()) {
    fail(`Refusing to replace non-directory ${kind}: ${target}`)
  }
  if (kind === 'plugin') {
    const manifest = path.join(target, 'plugin.yaml')
    requireFile(manifest, 'Installed Record manifest')
    if (!/^name:\s*['"]?record['"]?\s*$/m.test(fs.readFileSync(manifest, 'utf8'))) {
      fail(`Refusing to replace an unrecognized plugin: ${target}`)
    }
  } else if (kind === 'desktop plugin') {
    requireFile(path.join(target, 'plugin.js'), 'Installed Record Desktop plugin')
  } else if (kind === 'recorder') {
    const manifestPath = path.join(target, 'recorder-manifest.json')
    requireFile(manifestPath, 'Installed recorder manifest')
    if (JSON.parse(fs.readFileSync(manifestPath, 'utf8')).product !== 'record') {
      fail(`Refusing to replace an unrecognized recorder: ${target}`)
    }
  }
}

function replaceDirectories(items) {
  const committed = []
  try {
    for (const item of items) {
      assertReplaceable(item.target, item.kind)
      const backup = path.join(
        path.dirname(item.target),
        `.${path.basename(item.target)}.backup-${process.pid}-${Date.now()}`
      )
      if (fs.existsSync(item.target)) fs.renameSync(item.target, backup)
      else item.hadTarget = false
      item.backup = backup
      try {
        fs.renameSync(item.temporary, item.target)
      } catch (error) {
        if (fs.existsSync(backup) && !fs.existsSync(item.target)) {
          fs.renameSync(backup, item.target)
        }
        throw error
      }
      committed.push(item)
    }
  } catch (error) {
    for (const item of committed.reverse()) {
      fs.rmSync(item.target, { recursive: true, force: true })
      if (fs.existsSync(item.backup)) fs.renameSync(item.backup, item.target)
    }
    for (const item of items) {
      fs.rmSync(item.temporary, { recursive: true, force: true })
    }
    throw error
  }
  return () => {
    for (const item of items) {
      fs.rmSync(item.backup, { recursive: true, force: true })
    }
  }
}

function writeJsonAtomic(target, value) {
  fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 })
  const temporary = path.join(
    path.dirname(target),
    `.${path.basename(target)}.tmp-${process.pid}-${Date.now()}`
  )
  fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 })
  fs.renameSync(temporary, target)
  fs.chmodSync(target, 0o600)
}

function main() {
  if (unknownArguments.length) fail(`Unknown argument(s): ${unknownArguments.join(' ')}`)
  if (process.platform !== 'darwin' || process.arch !== 'arm64') {
    fail('Record clean build and run supports Apple-silicon macOS only.')
  }
  if (hermesHome !== path.resolve(defaultHermesHome)) {
    fail(
      `Record's local runner uses Hermes' default home only (${defaultHermesHome}); ` +
      `received ${hermesHome}.`
    )
  }
  requireFile(path.join(hostRoot, 'apps', 'desktop', 'package.json'), 'Hermes Desktop source')
  requireFile(hermesBin, 'Hermes launcher')
  requireFile(hermesPython, 'Hermes Python')
  validateRuntimeFiles(repoRoot)

  console.log('==> Cleaning Record build outputs')
  clean()
  console.log('==> Running Record tests')
  run(hermesPython, ['-m', 'pytest', '-q'])
  run('npm', ['run', 'test:chrome'])
  console.log('==> Building Record surfaces and signed recorder')
  run('npm', ['run', 'build'])
  run('npm', ['run', 'build:recorder'], {
    env: {
      ...process.env,
      HERMES_RECORDER_ALLOW_ADHOC_DEV:
        process.env.HERMES_RECORDER_ALLOW_ADHOC_DEV || '1'
    }
  })
  cleanCaches()

  const pluginTarget = path.join(hermesHome, 'plugins', 'record')
  const desktopTarget = path.join(hermesHome, 'desktop-plugins', 'record')
  const recorderTarget = path.join(hermesHome, 'record', 'native-recorder')
  const recorderBuild = path.join(repoRoot, 'dist', 'recorder-macos-arm64')
  const recorderManifestPath = path.join(recorderBuild, 'recorder-manifest.json')
  requireFile(recorderManifestPath, 'Recorder build manifest')
  const recorderManifest = JSON.parse(fs.readFileSync(recorderManifestPath, 'utf8'))
  if (recorderManifest.product !== 'record' || recorderManifest.release !== false) {
    fail('Local recorder output is not a Record development build.')
  }

  const pluginTemporary = stageDirectory(pluginTarget, temporary => {
    for (const relativePath of runtimeFiles) {
      copyFile(path.join(repoRoot, relativePath), path.join(temporary, relativePath))
    }
  })
  const desktopTemporary = stageDirectory(desktopTarget, temporary => {
    copyFile(
      path.join(repoRoot, 'desktop-plugin', 'dist', 'record', 'plugin.js'),
      path.join(temporary, 'plugin.js')
    )
  })
  const recorderTemporary = stageDirectory(recorderTarget, temporary => {
    fs.cpSync(recorderBuild, temporary, { recursive: true, force: true })
  })

  console.log('==> Installing Record into the default Hermes home')
  const launchServices =
    '/System/Library/Frameworks/CoreServices.framework/Frameworks/' +
    'LaunchServices.framework/Support/lsregister'
  const oldService = path.join(
    recorderTarget,
    'Hermes Recorder Service.app'
  )
  run('/usr/bin/pkill', ['-x', 'hermes-recorder-service'], { allowFailure: true })
  if (fs.existsSync(oldService)) {
    run(launchServices, ['-u', oldService], { allowFailure: true })
  }
  const finishReplacement = replaceDirectories([
    { kind: 'plugin', temporary: pluginTemporary, target: pluginTarget, hadTarget: true },
    { kind: 'desktop plugin', temporary: desktopTemporary, target: desktopTarget, hadTarget: true },
    { kind: 'recorder', temporary: recorderTemporary, target: recorderTarget, hadTarget: true }
  ])
  run(launchServices, [
    '-f',
    path.join(recorderTarget, 'Hermes Recorder Service.app')
  ])

  const extensionManifest = JSON.parse(
    fs.readFileSync(path.join(pluginTarget, 'chrome_capture', 'extension', 'manifest.json'), 'utf8')
  )
  writeJsonAtomic(path.join(hermesHome, 'record', 'installation.json'), {
    schemaVersion: 2,
    installedAt: new Date().toISOString(),
    version: productVersions.version,
    platform: 'darwin',
    arch: 'arm64',
    release: false,
    signingIdentity: recorderManifest.signingIdentity,
    teamId: recorderManifest.teamId,
    profile: 'default',
    hermesHome,
    pythonPlugin: pluginTarget,
    desktopPrimaryProfile: 'default',
    desktopPlugin: desktopTarget,
    recorderClient: path.join(recorderTarget, 'hermes-recorder'),
    recorderClientSha256: sha256(path.join(recorderTarget, 'hermes-recorder')),
    recorderManifestSha256: sha256(path.join(recorderTarget, 'recorder-manifest.json')),
    notarizationStatus: 'not-applicable-dev',
    notarizationSubmissionId: null,
    chromeMode: 'local',
    chromeExtensionId: chromeIdentity.extensionId,
    chromeExtensionVersion: String(extensionManifest.version),
    chromeStoreStatus: chromeIdentity.currentVersionPublished
      ? 'published'
      : 'update-required',
    chromePublishedVersion: chromeIdentity.publishedVersion,
    chromePublishedSourceCommit: chromeIdentity.publishedSourceCommit,
    chromeExtensionArchiveSha256: chromeIdentity.publishedArchiveSha256,
    pluginEnableAction: 'enabled'
  })
  run(hermesPython, [
    path.join(pluginTarget, 'chrome_capture', 'install_native_host.py'),
    '--customer-install',
    '--python', hermesPython,
    '--plugin-root', pluginTarget,
    '--hermes-home', hermesHome
  ])
  run('/bin/bash', [hermesBin, 'plugins', 'enable', 'record', '--no-allow-tool-override'], {
    env: { ...process.env, HERMES_HOME: hermesHome }
  })
  finishReplacement()

  const desktopEnv = {
    ...process.env,
    HERMES_HOME: hermesHome,
    // Local Record runs must never consume a distribution certificate by
    // electron-builder auto-discovery. Release signing is intentionally
    // isolated behind each repository's clean-tag release command.
    CSC_IDENTITY_AUTO_DISCOVERY: 'false',
    CSC_NAME: '',
    APPLE_SIGNING_IDENTITY: '',
    HERMES_RELEASE_BUILD: '0',
    HERMES_RELEASE_PHASE: ''
  }
  const desktopArgs = [
    'desktop',
    '--build-only',
    '--force-build',
    '--local-adhoc',
    '--hermes-root', hostRoot,
    '--cwd', repoRoot
  ]
  console.log('==> Clean-building Hermes Desktop from its source checkout')
  run(hermesBin, desktopArgs, { cwd: hostRoot, env: desktopEnv })
  if (buildOnly) {
    console.log('Record and Hermes Desktop clean build completed.')
    return
  }

  console.log('==> Launching Hermes Desktop with Record')
  const child = spawn(
    hermesBin,
    [
      'desktop',
      '--skip-build',
      '--local-adhoc',
      '--hermes-root', hostRoot,
      '--cwd', repoRoot
    ],
    {
      detached: true,
      stdio: 'ignore',
      cwd: hostRoot,
      env: desktopEnv
    }
  )
  child.unref()
  console.log(`Hermes Desktop launch requested (pid ${child.pid}).`)
}

try {
  main()
} catch (error) {
  console.error(`Record clean build and run failed: ${error.message}`)
  process.exitCode = 1
}
