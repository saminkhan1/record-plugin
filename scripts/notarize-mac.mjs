#!/usr/bin/env node

import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const distRoot = path.join(repoRoot, 'dist')
const packageRoot = path.join(distRoot, 'macos-arm64')
const installerAppName = 'Record Installer.app'
const installerApp = path.join(packageRoot, installerAppName)
const payloadRoot = path.join(installerApp, 'Contents', 'Resources', 'payload')
const manifestPath = path.join(payloadRoot, 'manifest.json')

function run(command, args, { capture = false } = {}) {
  const result = spawnSync(command, args, {
    encoding: 'utf8',
    stdio: capture ? ['ignore', 'pipe', 'pipe'] : 'inherit',
    shell: false
  })
  if (result.error) throw new Error(`${command} failed to start: ${result.error.message}`)
  if (result.status !== 0) {
    throw new Error(
      `${command} failed: ${String(result.stderr || result.stdout || `status ${result.status}`).trim()}`
    )
  }
  return String(result.stdout || '')
}

function sha256(target) {
  return createHash('sha256').update(fs.readFileSync(target)).digest('hex')
}

function filesUnder(root, current = root) {
  const result = []
  for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
    const target = path.join(current, entry.name)
    if (entry.isDirectory()) result.push(...filesUnder(root, target))
    else if (entry.isFile() && path.relative(root, target) !== 'manifest.json') {
      result.push(path.relative(root, target))
    } else if (!entry.isFile()) {
      throw new Error(`Package contains an unsupported filesystem entry: ${target}`)
    }
  }
  return result.sort()
}

function readJsonObject(target, label) {
  let value
  try {
    value = JSON.parse(fs.readFileSync(target, 'utf8'))
  } catch (error) {
    throw new Error(`${label} is unreadable: ${target}: ${error.message}`)
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object: ${target}`)
  }
  return value
}

function requirePayloadRelativePath(value, label) {
  if (
    typeof value !== 'string' ||
    !value ||
    value.includes('\0') ||
    path.posix.isAbsolute(value) ||
    path.posix.normalize(value) !== value ||
    value === '..' ||
    value.startsWith('../')
  ) {
    throw new Error(`${label} is not a normalized payload-relative path`)
  }
  return value
}

function sameStrings(left, right) {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  )
}

function payloadInventory(root) {
  return Object.fromEntries(
    filesUnder(root).map(relativePath => [
      relativePath,
      sha256(path.join(root, relativePath))
    ])
  )
}

function validateManifest(manifest, target) {
  const source = manifest.source
  if (
    manifest.schemaVersion !== 3 ||
    manifest.name !== 'record' ||
    typeof manifest.version !== 'string' ||
    !/^\d+\.\d+\.\d+$/.test(manifest.version) ||
    manifest.release !== true ||
    manifest.platform !== 'darwin' ||
    manifest.arch !== 'arm64' ||
    manifest.distribution?.customerEntry !== installerAppName ||
    typeof manifest.signingIdentity !== 'string' ||
    !manifest.signingIdentity ||
    typeof manifest.teamId !== 'string' ||
    !/^[A-Z0-9]{10}$/.test(manifest.teamId) ||
    !source ||
    typeof source !== 'object' ||
    Array.isArray(source) ||
    typeof source.commit !== 'string' ||
    !/^[0-9a-f]{40}$/.test(source.commit) ||
    source.tag !== `v${manifest.version}` ||
    !manifest.files ||
    typeof manifest.files !== 'object' ||
    Array.isArray(manifest.files)
  ) {
    throw new Error(`Release package is missing or invalid: ${target}`)
  }
  const recorderClient = requirePayloadRelativePath(
    manifest.recorderClient,
    'manifest.recorderClient'
  )
  if (!Object.hasOwn(manifest.files, recorderClient)) {
    throw new Error(`Release package omits its recorder client: ${target}`)
  }
}

function validatePayloadInventory(root, manifest, label) {
  const declaredFiles = Object.keys(manifest.files).sort()
  const actualFiles = filesUnder(root)
  if (!sameStrings(declaredFiles, actualFiles)) {
    throw new Error(`${label} file inventory does not match manifest.json`)
  }
  for (const relativePath of declaredFiles) {
    requirePayloadRelativePath(relativePath, `manifest.files[${relativePath}]`)
    const digest = manifest.files[relativePath]
    if (
      typeof digest !== 'string' ||
      !/^[0-9a-f]{64}$/.test(digest) ||
      sha256(path.join(root, relativePath)) !== digest
    ) {
      throw new Error(`${label} checksum mismatch: ${relativePath}`)
    }
  }
  return payloadInventory(root)
}

function requireSameInventory(expected, actual, label) {
  const expectedFiles = Object.keys(expected).sort()
  const actualFiles = Object.keys(actual).sort()
  if (
    !sameStrings(expectedFiles, actualFiles) ||
    expectedFiles.some(relativePath => expected[relativePath] !== actual[relativePath])
  ) {
    throw new Error(`${label} changed while staging the release`)
  }
}

function authorizeRecorderStapleChanges(
  originalInventory,
  candidateInventory,
  recorderServiceRelative
) {
  const ticketResource = path.posix.join(
    recorderServiceRelative,
    'Contents',
    'CodeResources'
  )
  const originalFiles = Object.keys(originalInventory).sort()
  const candidateFiles = Object.keys(candidateInventory).sort()
  for (const relativePath of originalFiles) {
    if (!Object.hasOwn(candidateInventory, relativePath)) {
      throw new Error(
        `Recorder stapling unexpectedly removed payload file: ${relativePath}`
      )
    }
  }
  for (const relativePath of candidateFiles) {
    const isNew = !Object.hasOwn(originalInventory, relativePath)
    const changed = (
      !isNew &&
      originalInventory[relativePath] !== candidateInventory[relativePath]
    )
    if ((isNew || changed) && relativePath !== ticketResource) {
      throw new Error(
        `Recorder stapling changed an unauthorized payload file: ${relativePath}`
      )
    }
  }

  // Preserve every reviewed digest verbatim. The only digest that can be
  // replaced or added is the recorder service's stapled ticket resource.
  const authorized = { ...originalInventory }
  if (Object.hasOwn(candidateInventory, ticketResource)) {
    authorized[ticketResource] = candidateInventory[ticketResource]
  }
  return Object.fromEntries(
    Object.entries(authorized).sort(([left], [right]) => left.localeCompare(right))
  )
}

function writeJsonAtomic(target, value) {
  const temporary = `${target}.tmp-${process.pid}-${Date.now()}`
  const mode = fs.statSync(target).mode & 0o777
  try {
    fs.writeFileSync(
      temporary,
      `${JSON.stringify(value, null, 2)}\n`,
      { flag: 'wx', mode }
    )
    fs.renameSync(temporary, target)
  } finally {
    fs.rmSync(temporary, { force: true })
  }
}

function submissionLogPath(label, version) {
  return path.join(
    distRoot,
    `notarization-${label}-${version}-${Date.now()}.json`
  )
}

function notarize(target, label, version, profile, temporaryRoot) {
  const submissionZip = path.join(temporaryRoot, `${label}.zip`)
  fs.rmSync(submissionZip, { force: true })
  run('/usr/bin/ditto', [
    '-c',
    '-k',
    '--sequesterRsrc',
    '--keepParent',
    target,
    submissionZip
  ])
  const rawSubmission = run(
    '/usr/bin/xcrun',
    [
      'notarytool',
      'submit',
      submissionZip,
      '--keychain-profile',
      profile,
      '--wait',
      '--output-format',
      'json'
    ],
    { capture: true }
  )
  const submission = JSON.parse(rawSubmission)
  if (submission.status !== 'Accepted' || typeof submission.id !== 'string') {
    throw new Error(
      `Apple notarization was not accepted for ${label} (status ${String(submission.status || 'unknown')})`
    )
  }

  const logPath = submissionLogPath(label, version)
  run('/usr/bin/xcrun', [
    'notarytool',
    'log',
    submission.id,
    logPath,
    '--keychain-profile',
    profile
  ])
  const notaryLog = JSON.parse(fs.readFileSync(logPath, 'utf8'))
  if (
    notaryLog.status !== 'Accepted' ||
    (Array.isArray(notaryLog.issues) && notaryLog.issues.length > 0)
  ) {
    throw new Error(`Accepted ${label} submission has issues; inspect ${logPath}`)
  }
  return {
    id: submission.id,
    logPath
  }
}

function signInstallerApp(target, identity) {
  run('/usr/bin/codesign', [
    '--force',
    '--options',
    'runtime',
    '--timestamp',
    '--identifier',
    'com.hermes.record.installer',
    '--sign',
    identity,
    target
  ])
  run('/usr/bin/codesign', ['--verify', '--deep', '--strict', '--verbose=4', target])
}

function promoteRelease({
  stagedPackageRoot,
  stagedArtifact,
  artifact,
  stagedReceipt,
  receipt,
  verifyPromoted
}) {
  const suffix = `${process.pid}-${Date.now()}`
  const packageBackup = path.join(
    distRoot,
    `.macos-arm64.notarize-backup-${suffix}`
  )
  const artifactBackup = path.join(
    distRoot,
    `.${path.basename(artifact)}.notarize-backup-${suffix}`
  )
  const receiptBackup = path.join(
    distRoot,
    `.${path.basename(receipt)}.notarize-backup-${suffix}`
  )
  let packageBackedUp = false
  let artifactBackedUp = false
  let receiptBackedUp = false
  let packagePromoted = false
  let artifactPromoted = false
  let receiptPromoted = false

  try {
    if (fs.existsSync(packageRoot)) {
      fs.renameSync(packageRoot, packageBackup)
      packageBackedUp = true
    }
    fs.renameSync(stagedPackageRoot, packageRoot)
    packagePromoted = true

    if (fs.existsSync(artifact)) {
      fs.renameSync(artifact, artifactBackup)
      artifactBackedUp = true
    }
    fs.renameSync(stagedArtifact, artifact)
    artifactPromoted = true

    if (fs.existsSync(receipt)) {
      fs.renameSync(receipt, receiptBackup)
      receiptBackedUp = true
    }
    fs.renameSync(stagedReceipt, receipt)
    receiptPromoted = true
    verifyPromoted()
  } catch (error) {
    const rollbackErrors = []
    if (receiptPromoted && fs.existsSync(receipt)) {
      try {
        fs.renameSync(receipt, stagedReceipt)
      } catch (rollbackError) {
        rollbackErrors.push(`receipt: ${rollbackError.message}`)
      }
    }
    if (
      receiptBackedUp &&
      fs.existsSync(receiptBackup) &&
      !fs.existsSync(receipt)
    ) {
      try {
        fs.renameSync(receiptBackup, receipt)
      } catch (rollbackError) {
        rollbackErrors.push(`receipt backup: ${rollbackError.message}`)
      }
    }
    if (artifactPromoted && fs.existsSync(artifact)) {
      try {
        fs.renameSync(artifact, stagedArtifact)
      } catch (rollbackError) {
        rollbackErrors.push(`artifact: ${rollbackError.message}`)
      }
    }
    if (
      artifactBackedUp &&
      fs.existsSync(artifactBackup) &&
      !fs.existsSync(artifact)
    ) {
      try {
        fs.renameSync(artifactBackup, artifact)
      } catch (rollbackError) {
        rollbackErrors.push(`artifact backup: ${rollbackError.message}`)
      }
    }
    if (packagePromoted && fs.existsSync(packageRoot)) {
      try {
        fs.renameSync(packageRoot, stagedPackageRoot)
      } catch (rollbackError) {
        rollbackErrors.push(`package: ${rollbackError.message}`)
      }
    }
    if (
      packageBackedUp &&
      fs.existsSync(packageBackup) &&
      !fs.existsSync(packageRoot)
    ) {
      try {
        fs.renameSync(packageBackup, packageRoot)
      } catch (rollbackError) {
        rollbackErrors.push(`package backup: ${rollbackError.message}`)
      }
    }
    const rollbackDetail = rollbackErrors.length
      ? `; rollback errors: ${rollbackErrors.join('; ')}`
      : ''
    throw new Error(`Release promotion failed: ${error.message}${rollbackDetail}`)
  }

  for (const backup of [
    packageBackedUp ? packageBackup : null,
    artifactBackedUp ? artifactBackup : null,
    receiptBackedUp ? receiptBackup : null
  ]) {
    if (!backup) continue
    try {
      fs.rmSync(backup, { recursive: true, force: true })
    } catch (error) {
      console.warn(
        `Release promoted but previous output could not be removed ${backup}: ${error.message}`
      )
    }
  }
}

if (process.platform !== 'darwin' || process.arch !== 'arm64') {
  throw new Error('Record notarization runs on Apple-silicon macOS only')
}
const profile = String(process.env.APPLE_NOTARY_PROFILE || '').trim()
if (!profile) {
  throw new Error(
    'APPLE_NOTARY_PROFILE must name credentials stored with `xcrun notarytool store-credentials`'
  )
}
if (!fs.statSync(installerApp, { throwIfNoEntry: false })?.isDirectory()) {
  throw new Error(`Canonical customer installer is missing: ${installerApp}`)
}
const originalManifest = readJsonObject(manifestPath, 'Release package manifest')
validateManifest(originalManifest, manifestPath)
const originalManifestSha256 = sha256(manifestPath)
const originalInventory = validatePayloadInventory(
  payloadRoot,
  originalManifest,
  'Canonical release payload'
)
const recorderServiceRelative = path.posix.join(
  path.posix.dirname(originalManifest.recorderClient),
  'Hermes Recorder Service.app'
)
const originalRecorderService = path.join(payloadRoot, recorderServiceRelative)
if (!fs.statSync(originalRecorderService, { throwIfNoEntry: false })?.isDirectory()) {
  throw new Error(`Packaged recorder service is missing: ${originalRecorderService}`)
}
run('/usr/bin/codesign', [
  '--verify',
  '--deep',
  '--strict',
  '--verbose=4',
  installerApp
])

fs.mkdirSync(distRoot, { recursive: true })
const temporaryRoot = fs.mkdtempSync(
  path.join(distRoot, '.record-notary-')
)
const stagedPackageRoot = path.join(temporaryRoot, 'macos-arm64')
const stagedInstallerApp = path.join(stagedPackageRoot, installerAppName)
const stagedPayloadRoot = path.join(
  stagedInstallerApp,
  'Contents',
  'Resources',
  'payload'
)
const stagedManifestPath = path.join(stagedPayloadRoot, 'manifest.json')

try {
  // ditto preserves the macOS metadata and bundle layout needed for signing
  // and stapling. The canonical package remains untouched until promotion.
  run('/usr/bin/ditto', [packageRoot, stagedPackageRoot])
  if (sha256(stagedManifestPath) !== originalManifestSha256) {
    throw new Error('Release manifest changed while copying to notarization staging')
  }
  const manifest = readJsonObject(stagedManifestPath, 'Staged release package manifest')
  validateManifest(manifest, stagedManifestPath)
  const stagedInventory = validatePayloadInventory(
    stagedPayloadRoot,
    manifest,
    'Staged release payload'
  )
  requireSameInventory(
    originalInventory,
    stagedInventory,
    'Release payload'
  )

  const recorderClient = path.join(stagedPayloadRoot, manifest.recorderClient)
  const recorderService = path.join(stagedPayloadRoot, recorderServiceRelative)
  run('/usr/bin/codesign', [
    '--verify',
    '--deep',
    '--strict',
    '--verbose=4',
    stagedInstallerApp
  ])

  // The recorder service is copied out of Installer.app at install time. Give
  // that nested app its own stapled ticket before sealing and notarizing the
  // outer customer installer, so first launch does not depend on Xcode or an
  // online ticket lookup.
  const recorderSubmission = notarize(
    recorderService,
    'recorder-service',
    manifest.version,
    profile,
    temporaryRoot
  )
  run('/usr/bin/xcrun', ['stapler', 'staple', '-v', recorderService])
  run('/usr/bin/xcrun', ['stapler', 'validate', '-v', recorderService])
  run('/usr/bin/codesign', [
    '--verify',
    '--deep',
    '--strict',
    '--verbose=4',
    recorderService
  ])

  // Do not bless an arbitrary staged-payload change. A stapled .app ticket is
  // stored in the nested service's Contents/CodeResources; every other
  // reviewed payload byte must remain exactly as packaged.
  const stapledInventory = payloadInventory(stagedPayloadRoot)
  manifest.files = authorizeRecorderStapleChanges(
    originalInventory,
    stapledInventory,
    recorderServiceRelative
  )
  writeJsonAtomic(stagedManifestPath, manifest)
  const authorizedInventory = validatePayloadInventory(
    stagedPayloadRoot,
    manifest,
    'Stapled release payload'
  )
  signInstallerApp(stagedInstallerApp, manifest.signingIdentity)
  requireSameInventory(
    authorizedInventory,
    validatePayloadInventory(
      stagedPayloadRoot,
      manifest,
      'Signed release payload'
    ),
    'Payload inventory after signing the outer installer'
  )

  const installerSubmission = notarize(
    stagedInstallerApp,
    'installer-app',
    manifest.version,
    profile,
    temporaryRoot
  )
  run('/usr/bin/xcrun', ['stapler', 'staple', '-v', stagedInstallerApp])
  run('/usr/bin/xcrun', ['stapler', 'validate', '-v', stagedInstallerApp])
  run('/usr/bin/codesign', [
    '--verify',
    '--deep',
    '--strict',
    '--verbose=4',
    stagedInstallerApp
  ])
  run('/usr/sbin/spctl', ['--assess', '--type', 'execute', '--verbose=4', stagedInstallerApp])
  // Gatekeeper assesses distributable app bundles, not standalone Mach-O
  // command-line tools. The recorder client is covered by the package's
  // hardened-runtime codesign and immutable payload checks above.
  run('/usr/sbin/spctl', ['--assess', '--type', 'execute', '--verbose=4', recorderService])
  requireSameInventory(
    authorizedInventory,
    validatePayloadInventory(
      stagedPayloadRoot,
      manifest,
      'Final notarized release payload'
    ),
    'Payload inventory after notarizing the outer installer'
  )

  const artifact = path.join(
    distRoot,
    `record-${manifest.version}-macos-arm64.zip`
  )
  const stagedArtifact = path.join(temporaryRoot, path.basename(artifact))
  run('/usr/bin/ditto', [
    '-c',
    '-k',
    '--sequesterRsrc',
    '--keepParent',
    stagedInstallerApp,
    stagedArtifact
  ])
  const receipt = path.join(
    distRoot,
    `record-${manifest.version}-macos-arm64.release.json`
  )
  const stagedReceipt = path.join(temporaryRoot, path.basename(receipt))
  const artifactSha256 = sha256(stagedArtifact)
  const artifactBytes = fs.statSync(stagedArtifact).size
  const payloadManifestSha256 = sha256(stagedManifestPath)
  const releaseReceipt = {
    schemaVersion: 1,
    product: 'record',
    version: manifest.version,
    platform: 'darwin',
    arch: 'arm64',
    source: manifest.source,
    teamId: manifest.teamId,
    artifact: {
      file: path.basename(artifact),
      bytes: artifactBytes,
      sha256: artifactSha256
    },
    payloadManifest: {
      path: path.posix.join(
        installerAppName,
        'Contents',
        'Resources',
        'payload',
        'manifest.json'
      ),
      sha256: payloadManifestSha256
    },
    notarization: {
      status: 'accepted-and-stapled',
      recorderService: {
        submissionId: recorderSubmission.id,
        status: 'Accepted',
        logFile: path.basename(recorderSubmission.logPath),
        logSha256: sha256(recorderSubmission.logPath)
      },
      installerApp: {
        submissionId: installerSubmission.id,
        status: 'Accepted',
        logFile: path.basename(installerSubmission.logPath),
        logSha256: sha256(installerSubmission.logPath)
      }
    }
  }
  fs.writeFileSync(
    stagedReceipt,
    `${JSON.stringify(releaseReceipt, null, 2)}\n`,
    { flag: 'wx', mode: 0o644 }
  )
  const releaseReceiptSha256 = sha256(stagedReceipt)
  promoteRelease({
    stagedPackageRoot,
    stagedArtifact,
    artifact,
    stagedReceipt,
    receipt,
    verifyPromoted() {
      const promotedReceipt = readJsonObject(
        receipt,
        'Promoted release receipt'
      )
      if (
        sha256(artifact) !== artifactSha256 ||
        fs.statSync(artifact).size !== artifactBytes ||
        sha256(receipt) !== releaseReceiptSha256 ||
        sha256(manifestPath) !== payloadManifestSha256 ||
        promotedReceipt.source?.commit !== manifest.source.commit ||
        promotedReceipt.source?.tag !== manifest.source.tag ||
        promotedReceipt.artifact?.sha256 !== artifactSha256
      ) {
        throw new Error('Promoted release bytes do not match their receipt')
      }
    }
  })
  console.log(JSON.stringify({
    artifact,
    artifactSha256,
    receipt,
    releaseReceiptSha256,
    installerApp,
    recorderSubmissionId: recorderSubmission.id,
    recorderLogPath: recorderSubmission.logPath,
    installerSubmissionId: installerSubmission.id,
    installerLogPath: installerSubmission.logPath,
    status: 'accepted-and-stapled'
  }, null, 2))
} finally {
  try {
    fs.rmSync(temporaryRoot, { recursive: true, force: true })
  } catch (error) {
    console.warn(
      `Notarization staging cleanup failed for ${temporaryRoot}: ${error.message}`
    )
  }
}
