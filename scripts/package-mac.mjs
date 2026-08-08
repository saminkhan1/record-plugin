#!/usr/bin/env node

import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildChromeArchive, chromePackageFiles } from './package-chrome.mjs'
import {
  requirePublishedChromeSource,
  requireReleaseSource,
  validateChromeExtensionIdentity,
  validateProductVersions
} from './release-contract.mjs'
import { runtimeFiles, validateRuntimeFiles } from './runtime-files.mjs'

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
validateRuntimeFiles(repoRoot)
const finalOutputRoot = path.join(repoRoot, 'dist', 'macos-arm64')
const outputRoot = path.join(
  repoRoot,
  'dist',
  `.macos-arm64.package-${process.pid}-${Date.now()}`
)
const installerAppName = 'Record Installer.app'
const installerApp = path.join(outputRoot, installerAppName)
const installerContents = path.join(installerApp, 'Contents')
const installerResources = path.join(installerContents, 'Resources')
const packagePayloadRoot = path.join(installerResources, 'payload')
const pythonPluginRoot = path.join(packagePayloadRoot, 'plugins', 'record')
const desktopPluginRoot = path.join(packagePayloadRoot, 'desktop-plugins', 'record')
const packagedRecorder = path.join(packagePayloadRoot, 'native', 'hermes-recorder')
const installerExecutable = path.join(installerContents, 'MacOS', 'record-installer')
const recorderRoot = path.join(repoRoot, 'dist', 'recorder-macos-arm64')

process.on('exit', () => {
  fs.rmSync(outputRoot, { recursive: true, force: true })
})

function sha256(target) {
  return createHash('sha256').update(fs.readFileSync(target)).digest('hex')
}

function copyFile(relativePath, destinationRoot = pythonPluginRoot) {
  const source = path.join(repoRoot, relativePath)
  if (!fs.statSync(source, { throwIfNoEntry: false })?.isFile()) {
    throw new Error(`Required package input is missing: ${relativePath}`)
  }
  const destination = path.join(destinationRoot, relativePath)
  fs.mkdirSync(path.dirname(destination), { recursive: true })
  fs.copyFileSync(source, destination)
  fs.chmodSync(destination, fs.statSync(source).mode & 0o777)
}

function filesUnder(root, current = root) {
  const result = []
  for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
    const target = path.join(current, entry.name)
    if (entry.isDirectory()) result.push(...filesUnder(root, target))
    else if (entry.isFile()) result.push(path.relative(root, target))
    else throw new Error(`Package contains an unsupported filesystem entry: ${target}`)
  }
  return result.sort()
}

function requireReleaseSignature(target, teamId) {
  const result = spawnSync(
    '/usr/bin/codesign',
    ['-d', '--verbose=4', target],
    { encoding: 'utf8', shell: false }
  )
  const details = `${result.stdout || ''}\n${result.stderr || ''}`
  if (
    result.status !== 0 ||
    !details.includes('flags=0x10000(runtime)') ||
    !details.includes(`TeamIdentifier=${teamId}`) ||
    !/^Timestamp=.+$/m.test(details)
  ) {
    throw new Error(
      `Release signature is missing hardened runtime, timestamp, or Team ${teamId}: ${target}`
    )
  }
}

function checkJavaScript(target) {
  const result = spawnSync(process.execPath, ['--check', target], {
    encoding: 'utf8',
    shell: false
  })
  if (result.status !== 0) {
    throw new Error(`JavaScript syntax check failed for ${target}: ${result.stderr || result.stdout}`)
  }
}

function checkPython(target) {
  const python = String(process.env.HERMES_RELEASE_PYTHON || 'python3').trim()
  const result = spawnSync(
    python,
    [
      '-c',
      'import pathlib,sys; compile(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"), sys.argv[1], "exec")',
      target
    ],
    {
      encoding: 'utf8',
      shell: false
    }
  )
  if (result.status !== 0) {
    throw new Error(`Python syntax check failed for ${target}: ${result.stderr || result.stdout}`)
  }
}

function signInstallerApp(target, identity) {
  const result = spawnSync(
    '/usr/bin/codesign',
    [
      '--force',
      '--options',
      'runtime',
      '--timestamp',
      '--identifier',
      'com.hermes.record.installer',
      '--sign',
      identity,
      target
    ],
    {
      encoding: 'utf8',
      shell: false,
      stdio: 'inherit'
    }
  )
  if (result.status !== 0) {
    throw new Error(`Could not sign the customer installer app: ${target}`)
  }
}

if (process.platform !== 'darwin' || process.arch !== 'arm64') {
  throw new Error('Record macOS packages are produced on Apple silicon only')
}
if (process.env.HERMES_RELEASE_BUILD !== '1') {
  throw new Error('package:mac requires HERMES_RELEASE_BUILD=1')
}
const expectedTeamId = String(process.env.APPLE_TEAM_ID || '').trim()
if (!/^[A-Z0-9]{10}$/.test(expectedTeamId)) {
  throw new Error('package:mac requires a valid 10-character APPLE_TEAM_ID')
}

const productVersions = validateProductVersions(repoRoot)
const releaseSource = requireReleaseSource(repoRoot, productVersions.version)
const chrome = validateChromeExtensionIdentity(repoRoot, { requirePublishedVersion: true })
requirePublishedChromeSource(repoRoot, chrome, chromePackageFiles)
const { archive: chromeArchive } = buildChromeArchive(
  path.join(repoRoot, 'chrome_capture', 'extension')
)
const chromeArchiveSha256 = createHash('sha256').update(chromeArchive).digest('hex')
if (chromeArchiveSha256 !== chrome.publishedArchiveSha256) {
  throw new Error(
    `Packaged Chrome bytes do not match the approved Store archive ${chrome.publishedArchiveSha256}`
  )
}

const recorderManifest = JSON.parse(
  fs.readFileSync(path.join(recorderRoot, 'recorder-manifest.json'), 'utf8')
)
if (
  recorderManifest.schemaVersion !== 2 ||
  recorderManifest.product !== 'record' ||
  recorderManifest.version !== productVersions.version ||
  recorderManifest.release !== true ||
  recorderManifest.platform !== 'darwin' ||
  recorderManifest.arch !== 'arm64' ||
  typeof recorderManifest.teamId !== 'string' ||
  recorderManifest.teamId !== expectedTeamId ||
  recorderManifest.source?.commit !== releaseSource.commit ||
  recorderManifest.source?.tag !== releaseSource.tag ||
  recorderManifest.installerLauncher?.identifier !== 'com.hermes.record.installer' ||
  recorderManifest.installerLauncher?.path !== 'record-installer' ||
  recorderManifest.installerLauncher?.sha256 !==
    sha256(path.join(recorderRoot, 'record-installer'))
) {
  throw new Error(
    'Recorder output is not a release-signed Apple-silicon build for APPLE_TEAM_ID'
  )
}

for (const target of [
  path.join(recorderRoot, 'hermes-recorder'),
  path.join(recorderRoot, 'Hermes Recorder Service.app', 'Contents', 'MacOS', 'hermes-recorder-service'),
  path.join(recorderRoot, 'record-installer'),
  path.join(recorderRoot, 'recorder-manifest.json'),
  path.join(repoRoot, 'desktop-plugin', 'dist', 'record', 'plugin.js')
]) {
  if (!fs.existsSync(target)) throw new Error(`Required build output is missing: ${target}`)
}
if (
  sha256(path.join(repoRoot, 'desktop-plugin', 'plugin.js')) !==
  sha256(path.join(repoRoot, 'desktop-plugin', 'dist', 'record', 'plugin.js'))
) {
  throw new Error('Desktop plugin build output does not match its reviewed source')
}
for (const target of [
  path.join(recorderRoot, 'hermes-recorder'),
  path.join(recorderRoot, 'Hermes Recorder Service.app', 'Contents', 'MacOS', 'hermes-recorder-service'),
  path.join(recorderRoot, 'record-installer')
]) {
  requireReleaseSignature(target, recorderManifest.teamId)
}
for (const target of [
  path.join(repoRoot, 'dashboard', 'dist', 'index.js'),
  path.join(repoRoot, 'desktop-plugin', 'dist', 'record', 'plugin.js'),
  path.join(repoRoot, 'chrome_capture', 'extension', 'src', 'background.js'),
  path.join(repoRoot, 'chrome_capture', 'extension', 'src', 'causality.js'),
  path.join(repoRoot, 'chrome_capture', 'extension', 'src', 'popup.js'),
  path.join(repoRoot, 'chrome_capture', 'extension', 'src', 'url-sanitizer.js'),
  path.join(repoRoot, 'chrome_capture', 'extension', 'src', 'user-events.js')
]) {
  checkJavaScript(target)
}
checkPython(path.join(repoRoot, 'scripts', 'install-mac.py'))

fs.rmSync(outputRoot, { recursive: true, force: true })
for (const relativePath of runtimeFiles) copyFile(relativePath)
for (const relativePath of runtimeFiles) {
  if (
    sha256(path.join(repoRoot, relativePath)) !==
    sha256(path.join(pythonPluginRoot, relativePath))
  ) {
    throw new Error(`Record runtime source changed while packaging: ${relativePath}`)
  }
}

fs.mkdirSync(packagedRecorder, { recursive: true })
fs.copyFileSync(
  path.join(recorderRoot, 'hermes-recorder'),
  path.join(packagedRecorder, 'hermes-recorder')
)
fs.chmodSync(path.join(packagedRecorder, 'hermes-recorder'), 0o755)
fs.cpSync(
  path.join(recorderRoot, 'Hermes Recorder Service.app'),
  path.join(packagedRecorder, 'Hermes Recorder Service.app'),
  { recursive: true }
)
fs.copyFileSync(
  path.join(recorderRoot, 'recorder-manifest.json'),
  path.join(packagedRecorder, 'recorder-manifest.json')
)
copyFile('scripts/install-mac.py', packagePayloadRoot)

fs.mkdirSync(desktopPluginRoot, { recursive: true })
fs.copyFileSync(
  path.join(repoRoot, 'desktop-plugin', 'dist', 'record', 'plugin.js'),
  path.join(desktopPluginRoot, 'plugin.js')
)

fs.mkdirSync(path.dirname(installerExecutable), { recursive: true })
fs.copyFileSync(
  path.join(recorderRoot, 'record-installer'),
  installerExecutable
)
fs.chmodSync(installerExecutable, 0o755)
const infoPlist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>record-installer</string>
  <key>CFBundleIdentifier</key>
  <string>com.hermes.record.installer</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>Record Installer</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>${productVersions.version}</string>
  <key>CFBundleVersion</key>
  <string>${productVersions.version}</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
  <key>LSUIElement</key>
  <true/>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
`
fs.writeFileSync(path.join(installerContents, 'Info.plist'), infoPlist)

const files = filesUnder(packagePayloadRoot)
const manifest = {
  schemaVersion: 3,
  name: 'record',
  version: productVersions.version,
  platform: 'darwin',
  arch: 'arm64',
  release: true,
  signingIdentity: recorderManifest.signingIdentity,
  teamId: recorderManifest.teamId,
  source: releaseSource,
  pythonPlugin: 'plugins/record',
  desktopPlugin: 'desktop-plugins/record',
  recorderClient: 'native/hermes-recorder/hermes-recorder',
  installer: 'scripts/install-mac.py',
  chromeExtension: 'plugins/record/chrome_capture/extension',
  chromeExtensionId: chrome.extensionId,
  chromeExtensionVersion: chrome.version,
  chromeStoreStatus: 'published',
  chromePublishedVersion: chrome.publishedVersion,
  chromePublishedSourceCommit: chrome.publishedSourceCommit,
  chromeExtensionArchiveSha256: chromeArchiveSha256,
  distribution: {
    customerEntry: installerAppName,
    notarization: 'release-gate'
  },
  requiredHermesCapabilities: [
    'plugin_llm',
    'plugin_agent',
    'plugin_profile',
    'plugin_tool_dispatch',
    'builtin_tool_discovery',
    'plugin_hooks',
    'atomic_webhook_mutation'
  ],
  files: Object.fromEntries(
    files.map(relativePath => [
      relativePath,
      sha256(path.join(packagePayloadRoot, relativePath))
    ])
  )
}
fs.writeFileSync(
  path.join(packagePayloadRoot, 'manifest.json'),
  `${JSON.stringify(manifest, null, 2)}\n`
)
const finalSource = requireReleaseSource(repoRoot, productVersions.version)
if (
  finalSource.commit !== releaseSource.commit ||
  finalSource.tag !== releaseSource.tag
) {
  throw new Error('Record release source changed while packaging')
}
signInstallerApp(installerApp, recorderManifest.signingIdentity)
requireReleaseSignature(installerApp, recorderManifest.teamId)
const verifyInstaller = spawnSync(
  '/usr/bin/codesign',
  ['--verify', '--deep', '--strict', '--verbose=4', installerApp],
  { encoding: 'utf8', shell: false, stdio: 'inherit' }
)
if (verifyInstaller.status !== 0) {
  throw new Error(`Customer installer signature verification failed: ${installerApp}`)
}

const backupRoot = path.join(
  path.dirname(finalOutputRoot),
  `.macos-arm64.backup-${process.pid}-${Date.now()}`
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
      `Package completed but could not remove the previous package backup ${backupRoot}: ${error.message}`
    )
  }
}

console.log(JSON.stringify({
  outputRoot: finalOutputRoot,
  version: manifest.version,
  source: manifest.source,
  files: files.length,
  chromeExtensionId: chrome.extensionId,
  chromeExtensionVersion: chrome.version,
  chromeExtensionArchiveSha256: chromeArchiveSha256
}, null, 2))
