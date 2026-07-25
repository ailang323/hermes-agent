import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { buildManagedUpdateCommand, parseManagedUpdateStatus } from './custom-update-handoff'

test('managed update command delegates the complete transaction and records its exit status', () => {
  const script = buildManagedUpdateCommand({
    updater: "/Users/example/Hermes user's bin/hermes-longer-update",
    runDir: "/Users/example/Hermes user's runs/desktop-button-1",
    hermesHome: "/Users/example/Hermes user's home"
  })

  assert.match(script, /^#!\/bin\/bash/m)
  assert.match(script, /set -o pipefail/)
  assert.match(script, /export LANG=en_US\.UTF-8/)
  assert.match(script, /export HERMES_HOME=/)
  assert.match(script, /trap finish EXIT/)
  assert.match(script, /install\.log/)
  assert.match(script, /status/)
  assert.match(script, /PIPESTATUS\[0\]/)
  assert.match(script, /hermes-longer-update/)
  assert.doesNotMatch(script, /--no-source-update|--no-install|ditto/)
  assert.match(script, /Hermes user'\\''s bin/)
})

test('managed update status parser distinguishes running, success, and failure', () => {
  assert.deepEqual(parseManagedUpdateStatus(''), { state: 'running' })
  assert.deepEqual(parseManagedUpdateStatus('finished_at=2026-07-27 08:00:00 CST\n'), { state: 'running' })
  assert.deepEqual(parseManagedUpdateStatus('exit_code=0\nfinished_at=2026-07-27 08:00:00 CST\n'), {
    state: 'succeeded',
    exitCode: 0
  })
  assert.deepEqual(parseManagedUpdateStatus('exit_code=17\nfinished_at=2026-07-27 08:00:00 CST\n'), {
    state: 'failed',
    exitCode: 17
  })
})

test('managed update command preserves the real updater exit code on disk', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-managed-update-'))
  const updater = path.join(root, 'fake updater.sh')
  const runDir = path.join(root, 'run dir')
  const commandPath = path.join(root, 'run.command')

  try {
    fs.writeFileSync(updater, '#!/bin/bash\nprintf "fake updater ran\\n"\nexit 17\n', { mode: 0o700 })
    fs.writeFileSync(
      commandPath,
      buildManagedUpdateCommand({ updater, runDir, hermesHome: path.join(root, 'home') }),
      { mode: 0o700 }
    )

    assert.throws(() => execFileSync('/bin/bash', [commandPath], { stdio: 'pipe' }))
    assert.deepEqual(parseManagedUpdateStatus(fs.readFileSync(path.join(runDir, 'status'), 'utf8')), {
      state: 'failed',
      exitCode: 17
    })
    assert.match(fs.readFileSync(path.join(runDir, 'install.log'), 'utf8'), /fake updater ran/)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
