import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

export interface ManagedUpdateHealthRequest {
  file: string
  token: string
}

const HEALTH_FILE_PREFIX = 'hermes-managed-update-health-'
const HEALTH_FILE_PATTERN = /^hermes-managed-update-health-[A-Za-z0-9._-]+\.json$/
const TOKEN_PATTERN = /^[0-9a-f]{64}$/

function argumentValue(argv: string[], name: string): string | null {
  const index = argv.indexOf(name)

  if (index < 0 || index + 1 >= argv.length) {
    return null
  }

  return argv[index + 1]
}

export function parseManagedUpdateHealthRequest(
  argv: string[],
  tempDirectory = os.tmpdir()
): ManagedUpdateHealthRequest | null {
  const requestedFile = argumentValue(argv, '--managed-update-health-file')
  const token = argumentValue(argv, '--managed-update-health-token')

  if (!requestedFile || !token || !TOKEN_PATTERN.test(token)) {
    return null
  }

  const file = path.resolve(requestedFile)
  const allowedDirectory = fs.realpathSync(tempDirectory)
  let requestedDirectory: string

  try {
    requestedDirectory = fs.realpathSync(path.dirname(file))
  } catch {
    return null
  }

  if (
    requestedDirectory !== allowedDirectory ||
    !path.basename(file).startsWith(HEALTH_FILE_PREFIX) ||
    !HEALTH_FILE_PATTERN.test(path.basename(file))
  ) {
    return null
  }

  return { file, token }
}

export function publishManagedUpdateHealth(
  request: ManagedUpdateHealthRequest,
  pid = process.pid,
  now = Date.now()
): void {
  const temporary = `${request.file}.tmp-${pid}-${now}`
  const payload = `${JSON.stringify({ token: request.token, pid, readyAt: now })}\n`
  let descriptor: number | null = null

  try {
    descriptor = fs.openSync(temporary, 'wx', 0o600)
    fs.writeFileSync(descriptor, payload, 'utf8')
    fs.fsyncSync(descriptor)
    fs.closeSync(descriptor)
    descriptor = null
    fs.linkSync(temporary, request.file)
  } catch (error) {
    if (descriptor !== null) {
      try {
        fs.closeSync(descriptor)
      } catch {
        // Preserve the operation error rather than masking it with cleanup failure.
      }
    }

    try {
      fs.unlinkSync(temporary)
    } catch {
      // Preserve the operation error; stale exclusive temp files are never reused.
    }

    throw error
  }

  fs.unlinkSync(temporary)
}
