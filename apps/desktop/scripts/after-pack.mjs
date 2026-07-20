/**
 * after-pack.mjs — electron-builder afterPack hook.
 *
 * Stamps the Hermes icon + identity onto the packed Windows Hermes.exe via
 * rcedit (delegated to set-exe-identity.mjs). This runs for EVERY packed build
 * — first install, `hermes desktop`, the installer's --update rebuild, and a
 * dev's manual `npm run pack` — so the branded exe can never silently revert
 * to the stock "Electron" icon/name (the bug when the stamp lived only in
 * install.ps1, which the update path doesn't use).
 *
 * On macOS, electron-builder's ASAR unpack step can reset node-pty's
 * `spawn-helper` to 0644 after staging set it to 0755. The embedded terminal
 * cannot start without that executable, so afterPack restores 0755 on the
 * final bundle and fails the pack if the helper is missing.
 *
 * On Windows, this stamps the Hermes icon + identity onto the packed
 * Hermes.exe via rcedit. That cosmetic step remains best-effort: a failure
 * keeps the stock icon but does not make the application unusable.
 *
 * electron-builder passes a context with:
 *   - electronPlatformName: 'win32' | 'darwin' | 'linux'
 *   - appOutDir:            the unpacked app directory for this target
 *   - packager.appInfo.productFilename: the exe basename (e.g. 'Hermes')
 */

import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

import { stampExeIdentity } from './set-exe-identity.mjs'

function makePackagedSpawnHelpersExecutable(appOutDir, productName) {
  const appBundle = path.join(appOutDir, `${productName}.app`)
  const nodePtyRoot = path.join(
    appBundle,
    'Contents',
    'Resources',
    'app.asar.unpacked',
    'dist',
    'node_modules',
    'node-pty'
  )
  let fixed = 0

  function visit(dir) {
    if (!fs.existsSync(dir)) return
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const filePath = path.join(dir, entry.name)
      if (entry.isDirectory()) {
        visit(filePath)
      } else if (entry.name === 'spawn-helper') {
        fs.chmodSync(filePath, 0o755)
        fixed += 1
      }
    }
  }

  visit(nodePtyRoot)
  if (fixed === 0) {
    throw new Error(`[after-pack] packaged node-pty spawn-helper missing under ${nodePtyRoot}`)
  }
}

export function signMacApp(appBundle) {
  const sign = spawnSync(
    '/usr/bin/codesign',
    ['--force', '--deep', '--sign', '-', '--timestamp=none', appBundle],
    { encoding: 'utf8' }
  )

  if (sign.error || sign.status !== 0) {
    const detail =
      sign.error?.message || sign.stderr?.trim() || sign.stdout?.trim() || `exit ${sign.status}`
    throw new Error(`[after-pack] ad-hoc signing failed for ${appBundle}: ${detail}`)
  }

  const verify = spawnSync(
    '/usr/bin/codesign',
    ['--verify', '--deep', '--strict', '--verbose=2', appBundle],
    { encoding: 'utf8' }
  )

  if (verify.error || verify.status !== 0) {
    const detail =
      verify.error?.message || verify.stderr?.trim() || verify.stdout?.trim() || `exit ${verify.status}`
    throw new Error(`[after-pack] strict signature verification failed for ${appBundle}: ${detail}`)
  }
}

export default async function afterPack(context, dependencies = {}) {
  if (context.electronPlatformName === 'darwin') {
    const productName = context.packager?.appInfo?.productFilename || 'Hermes'
    const appBundle = path.join(context.appOutDir, `${productName}.app`)
    makePackagedSpawnHelpersExecutable(context.appOutDir, productName)
    const signer = dependencies.signMacApp || signMacApp
    signer(appBundle)
    return
  }

  if (context.electronPlatformName !== 'win32') {
    return
  }

  const productName = context.packager?.appInfo?.productFilename || 'Hermes'
  const exe = path.join(context.appOutDir, `${productName}.exe`)
  const desktopRoot = path.resolve(import.meta.dirname, '..')

  try {
    await stampExeIdentity(exe, desktopRoot)
  } catch (err) {
    // Never fail the build over a cosmetic stamp.
    console.warn(`[after-pack] exe identity stamp failed (${err.message}); Hermes.exe keeps the stock Electron icon`)
  }
}
