import fs from 'node:fs'

export interface ManagedUpdateHealthRequest {
  fd: number
}

const HEALTH_FD_ARGUMENT = '--managed-update-health-fd'
const MIN_HEALTH_FD = 3
const MAX_HEALTH_FD = 1_048_576

function argumentValue(argv: string[], name: string): string | null {
  const index = argv.indexOf(name)

  if (index < 0 || index + 1 >= argv.length) {
    return null
  }

  return argv[index + 1]
}

export function parseManagedUpdateHealthRequest(argv: string[]): ManagedUpdateHealthRequest | null {
  const raw = argumentValue(argv, HEALTH_FD_ARGUMENT)

  if (!raw || !/^\d+$/.test(raw)) {
    return null
  }

  const fd = Number(raw)

  if (!Number.isSafeInteger(fd) || fd < MIN_HEALTH_FD || fd > MAX_HEALTH_FD) {
    return null
  }

  return { fd }
}

export function publishManagedUpdateHealth(
  request: ManagedUpdateHealthRequest,
  pid = process.pid,
  now = Date.now()
): void {
  const payload = `${JSON.stringify({ pid, readyAt: now })}\n`

  try {
    fs.writeFileSync(request.fd, payload, 'utf8')
  } finally {
    fs.closeSync(request.fd)
  }
}
