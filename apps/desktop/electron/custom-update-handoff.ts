type ManagedUpdateCommandOptions = {
  updater: string
  runDir: string
  hermesHome: string
}

type ManagedUpdateStatus =
  | { state: 'running' }
  | { state: 'succeeded'; exitCode: 0 }
  | { state: 'failed'; exitCode: number }

function shellQuote(value: string) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`
}

function buildManagedUpdateCommand({ updater, runDir, hermesHome }: ManagedUpdateCommandOptions) {
  const logPath = `${runDir}/install.log`
  const statusPath = `${runDir}/status`

  return `#!/bin/bash
set -o pipefail
export LANG=en_US.UTF-8
export HERMES_HOME=${shellQuote(hermesHome)}
RUN_DIR=${shellQuote(runDir)}
LOG=${shellQuote(logPath)}
STATUS=${shellQuote(statusPath)}
UPDATER=${shellQuote(updater)}
mkdir -p "$RUN_DIR"
rm -f "$STATUS" "$STATUS.tmp.$$"
: > "$LOG"
finish() {
  code=$?
  trap - EXIT
  {
    printf 'exit_code=%s\\n' "$code"
    printf 'finished_at=%s\\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
  } > "$STATUS.tmp.$$"
  mv "$STATUS.tmp.$$" "$STATUS"
}
trap finish EXIT
"$UPDATER" 2>&1 | /usr/bin/tee -a "$LOG"
exit "${'${PIPESTATUS[0]}'}"
`
}

function parseManagedUpdateStatus(value: string): ManagedUpdateStatus {
  const match = String(value || '').match(/(?:^|\n)exit_code=(-?\d+)(?:\n|$)/)

  if (!match) {
    return { state: 'running' }
  }

  const exitCode = Number.parseInt(match[1], 10)

  return exitCode === 0 ? { state: 'succeeded', exitCode: 0 } : { state: 'failed', exitCode }
}

export { buildManagedUpdateCommand, parseManagedUpdateStatus, shellQuote }
export type { ManagedUpdateCommandOptions, ManagedUpdateStatus }
