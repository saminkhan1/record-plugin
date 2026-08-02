import fs from 'node:fs'
import path from 'node:path'

export const runtimeFiles = [
  '__init__.py',
  'plugin.yaml',
  'chrome_capture.py',
  'record_recorder.py',
  'hermes_recorder.py',
  'recording_bundle.py',
  'record/__init__.py',
  'record/compiler.py',
  'record/drafting.py',
  'record/evidence.py',
  'record/hermes.py',
  'record/recording.py',
  'record/review_receipt.py',
  'record/planner.py',
  'record/runtime.py',
  'record/trace_schema.py',
  'record/webhook_filters.py',
  'dashboard/manifest.json',
  'dashboard/plugin_api.py',
  'dashboard/dist/index.js',
  'chrome_capture/install_native_host.py',
  'chrome_capture/native_host_launcher.py',
  'chrome_capture/native_host.py',
  'chrome_capture/extension/manifest.json',
  'chrome_capture/extension/store-identity.json',
  'chrome_capture/extension/icons/icon128.png',
  'chrome_capture/extension/src/background.js',
  'chrome_capture/extension/src/causality.js',
  'chrome_capture/extension/src/popup.html',
  'chrome_capture/extension/src/popup.js',
  'chrome_capture/extension/src/user-events.js'
]

const IGNORED_RUNTIME_DIRECTORIES = new Set([
  '.git',
  '__pycache__',
  'dist',
  'node_modules',
  'tests'
])
export function validateRuntimeFiles(repoRoot) {
  const listed = new Set(runtimeFiles)
  const missingInputs = runtimeFiles.filter(relativePath => {
    const target = path.join(repoRoot, relativePath)
    return !fs.statSync(target, { throwIfNoEntry: false })?.isFile()
  })
  const unlistedPythonFiles = fs.readdirSync(repoRoot, { recursive: true })
    .filter(relativePath => relativePath.endsWith('.py'))
    .map(relativePath => relativePath.split(path.sep).join('/'))
    .filter(relativePath => relativePath !== 'scripts/install-mac.py')
    .filter(relativePath => !relativePath.split('/').some(part => IGNORED_RUNTIME_DIRECTORIES.has(part)))
    .filter(relativePath => !listed.has(relativePath))

  if (missingInputs.length || unlistedPythonFiles.length) {
    const details = []
    if (missingInputs.length) {
      details.push(`missing runtime inputs: ${missingInputs.join(', ')}`)
    }
    if (unlistedPythonFiles.length) {
      details.push(`unlisted runtime Python files: ${unlistedPythonFiles.join(', ')}`)
    }
    throw new Error(`Record runtime file inventory is incomplete (${details.join('; ')})`)
  }
}
