import type { ChildProcess } from 'node:child_process'
import type { Readable, Writable } from 'node:stream'

export interface ManagedInstallApprovalPayload {
  confirmationToken: string
  candidateSha: string
  artifactSha256: string
  verificationReportSha256: string
}

const TOKEN_PATTERN = /^[0-9a-f]{64}$/
const SHA_PATTERN = /^[0-9a-f]{40}$/
const MAX_ACK_BYTES = 8192

function childPipe<T>(child: ChildProcess, index: number, kind: string): T {
  const pipe = child.stdio[index]

  if (!pipe) {
    throw new Error(`Managed update installer ${kind} pipe is unavailable.`)
  }

  return pipe as T
}

export function writeManagedInstallApproval(
  child: ChildProcess,
  approval: ManagedInstallApprovalPayload
): Promise<void> {
  if (
    !TOKEN_PATTERN.test(approval.confirmationToken) ||
    !SHA_PATTERN.test(approval.candidateSha) ||
    !TOKEN_PATTERN.test(approval.artifactSha256) ||
    !TOKEN_PATTERN.test(approval.verificationReportSha256)
  ) {
    throw new Error('Managed update install approval payload is invalid.')
  }

  const pipe = childPipe<Writable>(child, 3, 'approval')

  return new Promise<void>((resolve, reject) => {
    let settled = false

    const cleanup = () => {
      pipe.off('error', onError)
      pipe.off('finish', onFinish)
      pipe.off('close', onClose)
    }

    const finish = (error?: Error) => {
      if (settled) {
        return
      }

      settled = true
      cleanup()
      error ? reject(error) : resolve()
    }

    const onError = (error: Error) => finish(error)
    const onFinish = () => finish()
    const onClose = () => finish(new Error('Managed update installer approval pipe closed before finishing.'))

    pipe.once('error', onError)
    pipe.once('finish', onFinish)
    pipe.once('close', onClose)

    try {
      pipe.end(
        JSON.stringify({
          confirmation_token: approval.confirmationToken,
          candidate_sha: approval.candidateSha,
          artifact_sha256: approval.artifactSha256,
          verification_report_sha256: approval.verificationReportSha256
        })
      )
    } catch (error) {
      finish(error instanceof Error ? error : new Error(String(error)))
    }
  })
}

export async function terminateManagedInstaller(child: ChildProcess, timeoutMs = 2000): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return
  }

  await new Promise<void>(resolve => {
    let settled = false
    let escalationTimer: NodeJS.Timeout | undefined
    let finalTimer: NodeJS.Timeout | undefined

    const finish = () => {
      if (settled) {
        return
      }

      settled = true

      if (escalationTimer) {
        clearTimeout(escalationTimer)
      }

      if (finalTimer) {
        clearTimeout(finalTimer)
      }

      child.off('close', finish)
      resolve()
    }

    child.once('close', finish)

    if (child.exitCode !== null || child.signalCode !== null) {
      finish()

      return
    }

    try {
      child.kill('SIGTERM')
    } catch {
      // Continue to bounded escalation so cleanup never blocks invalidation.
    }

    escalationTimer = setTimeout(() => {
      if (child.exitCode === null && child.signalCode === null) {
        try {
          child.kill('SIGKILL')
        } catch {
          // The final timer still bounds cleanup when the process cannot be signalled.
        }
      }

      finalTimer = setTimeout(finish, timeoutMs)
    }, timeoutMs)
  })
}

export async function waitForManagedInstallerReady(
  child: ChildProcess,
  candidateId: string,
  timeoutMs = 5000
): Promise<void> {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(candidateId) || !Number.isInteger(child.pid)) {
    throw new Error('Managed update installer identity is invalid.')
  }

  const pipe = childPipe<Readable>(child, 4, 'readiness')

  await new Promise<void>((resolve, reject) => {
    let buffer = ''
    let settled = false
    const timer = setTimeout(() => finish(new Error('Managed update installer readiness timed out.')), timeoutMs)

    const cleanup = () => {
      clearTimeout(timer)
      pipe.off('data', onData)
      pipe.off('error', onPipeError)
      child.off('error', onChildError)
      child.off('exit', onExit)
    }

    const finish = (error?: Error) => {
      if (settled) {
        return
      }

      settled = true
      cleanup()
      error ? reject(error) : resolve()
    }

    const consume = () => {
      const newline = buffer.indexOf('\n')

      if (newline < 0) {
        return
      }

      const line = buffer.slice(0, newline)
      let payload: unknown

      try {
        payload = JSON.parse(line)
      } catch {
        finish(new Error('Managed update installer emitted invalid readiness JSON.'))

        return
      }

      if (
        typeof payload !== 'object' ||
        payload === null ||
        (payload as { event?: unknown }).event !== 'installer-ready' ||
        (payload as { candidate_id?: unknown }).candidate_id !== candidateId ||
        (payload as { pid?: unknown }).pid !== child.pid
      ) {
        finish(new Error('Managed update installer readiness identity does not match.'))

        return
      }

      finish()
    }

    const onData = (chunk: Buffer | string) => {
      buffer += chunk.toString()

      if (Buffer.byteLength(buffer, 'utf8') > MAX_ACK_BYTES) {
        finish(new Error('Managed update installer readiness payload is oversized.'))

        return
      }

      consume()
    }

    const onPipeError = (error: Error) => finish(error)
    const onChildError = (error: Error) => finish(error)

    const onExit = (code: null | number, signal: NodeJS.Signals | null) =>
      finish(
        new Error(
          `Managed update installer exited before readiness (code ${String(code)}, signal ${String(signal)}).`
        )
      )

    pipe.on('data', onData)
    pipe.once('error', onPipeError)
    child.once('error', onChildError)
    child.once('exit', onExit)
  })
}
