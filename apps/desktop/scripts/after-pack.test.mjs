import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'

import afterPack from './after-pack.mjs'

test('afterPack keeps the packaged macOS node-pty spawn-helper executable', async () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-after-pack-'))

  try {
    const appOutDir = path.join(tempRoot, 'mac-arm64')
    const appBundle = path.join(appOutDir, 'Hermes.app')
    const helper = path.join(
      appBundle,
      'Contents',
      'Resources',
      'app.asar.unpacked',
      'dist',
      'node_modules',
      'node-pty',
      'prebuilds',
      'darwin-arm64',
      'spawn-helper'
    )

    fs.mkdirSync(path.dirname(helper), { recursive: true })
    fs.writeFileSync(helper, '#!/bin/sh\nexit 0\n', { mode: 0o644 })

    await afterPack({ appOutDir, electronPlatformName: 'darwin' })

    assert.notEqual(fs.statSync(helper).mode & 0o111, 0, 'spawn-helper must retain an executable bit')
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})
