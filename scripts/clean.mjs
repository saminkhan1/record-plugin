#!/usr/bin/env node

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptPath = fileURLToPath(import.meta.url)
const repoRoot = path.resolve(path.dirname(scriptPath), '..')
const cacheNames = new Set([
  '__pycache__',
  '.pytest_cache',
  '.mypy_cache',
  '.ruff_cache'
])
const skippedNames = new Set(['.git', '.venv', 'node_modules'])

export function cleanCaches(root = repoRoot) {
  let removed = 0
  function visit(current) {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const target = path.join(current, entry.name)
      if (!entry.isDirectory() || entry.isSymbolicLink()) continue
      if (skippedNames.has(entry.name)) continue
      if (cacheNames.has(entry.name)) {
        fs.rmSync(target, { recursive: true, force: true })
        removed += 1
        continue
      }
      visit(target)
    }
  }
  visit(root)
  return removed
}

export function clean() {
  const outputs = [
    path.join(repoRoot, 'dist'),
    path.join(repoRoot, 'desktop-plugin', 'dist'),
    path.join(repoRoot, 'native', 'recorder-service', '.build')
  ]
  let removedOutputs = 0
  for (const target of outputs) {
    if (!fs.existsSync(target)) continue
    fs.rmSync(target, { recursive: true, force: true })
    removedOutputs += 1
  }
  const removedCaches = cleanCaches()
  console.log(
    `Cleaned Record outputs (${removedOutputs}) and cache directories (${removedCaches}).`
  )
}

if (process.argv[1] && path.resolve(process.argv[1]) === scriptPath) clean()
