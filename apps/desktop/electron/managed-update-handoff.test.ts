import { EventEmitter } from 'node:events'
import { PassThrough, Writable } from 'node:stream'

import { describe, expect, it, vi } from 'vitest'

import {
  terminateManagedInstaller,
  waitForManagedInstallerReady,
  writeManagedInstallApproval
} from './managed-update-handoff'

function fakeChild(pid = 4321) {
  const child = new EventEmitter() as EventEmitter & {
    pid: number
    stdio: Array<null | PassThrough>
  }

  child.pid = pid
  child.stdio = [null, null, null, new PassThrough(), new PassThrough()]

  return child
}

describe('managed update installer handoff', () => {
  it('sends the capability only through the private approval pipe', async () => {
    const child = fakeChild()
    const chunks: Buffer[] = []
    child.stdio[3]?.on('data', chunk => chunks.push(Buffer.from(chunk)))
    const ended = new Promise(resolve => child.stdio[3]?.once('end', resolve))

    await writeManagedInstallApproval(child as never, {
      confirmationToken: 'a'.repeat(64),
      candidateSha: 'b'.repeat(40),
      artifactSha256: 'c'.repeat(64),
      verificationReportSha256: 'd'.repeat(64)
    })
    await ended

    expect(JSON.parse(Buffer.concat(chunks).toString('utf8'))).toEqual({
      confirmation_token: 'a'.repeat(64),
      candidate_sha: 'b'.repeat(40),
      artifact_sha256: 'c'.repeat(64),
      verification_report_sha256: 'd'.repeat(64)
    })
  })

  it('rejects asynchronous approval-pipe write failures instead of emitting an unhandled error', async () => {
    const child = fakeChild()
    child.stdio[3] = new Writable({
      write(_chunk, _encoding, callback) {
        callback(Object.assign(new Error('broken approval pipe'), { code: 'EPIPE' }))
      }
    }) as PassThrough

    await expect(
      writeManagedInstallApproval(child as never, {
        confirmationToken: 'a'.repeat(64),
        candidateSha: 'b'.repeat(40),
        artifactSha256: 'c'.repeat(64),
        verificationReportSha256: 'd'.repeat(64)
      })
    ).rejects.toMatchObject({ code: 'EPIPE' })
  })

  it('waits for a failed installer child to close after termination', async () => {
    const child = new EventEmitter() as EventEmitter & {
      exitCode: null | number
      signalCode: NodeJS.Signals | null
      kill: (signal?: NodeJS.Signals | number) => boolean
    }

    child.exitCode = null
    child.signalCode = null
    child.kill = vi.fn(() => {
      queueMicrotask(() => {
        child.signalCode = 'SIGTERM'
        child.emit('close', null, 'SIGTERM')
      })

      return true
    })

    await terminateManagedInstaller(child as never, 50)

    expect(child.kill).toHaveBeenCalledWith('SIGTERM')
  })

  it('bounds cleanup even when installer signalling throws', async () => {
    const child = new EventEmitter() as EventEmitter & {
      exitCode: null | number
      signalCode: NodeJS.Signals | null
      kill: (signal?: NodeJS.Signals | number) => boolean
    }

    child.exitCode = null
    child.signalCode = null
    child.kill = vi.fn(() => {
      throw new Error('cannot signal child')
    })

    await expect(terminateManagedInstaller(child as never, 1)).resolves.toBeUndefined()
    expect(child.kill).toHaveBeenNthCalledWith(1, 'SIGTERM')
    expect(child.kill).toHaveBeenNthCalledWith(2, 'SIGKILL')
  })

  it('accepts only a matching child pid and candidate readiness acknowledgement', async () => {
    const child = fakeChild()
    const ready = waitForManagedInstallerReady(child as never, 'candidate-1', 1000)

    child.stdio[4]?.write(
      `${JSON.stringify({ event: 'installer-ready', candidate_id: 'candidate-1', pid: child.pid })}\n`
    )

    await expect(ready).resolves.toBeUndefined()
  })

  it('fails closed when the installer exits before acknowledging readiness', async () => {
    const child = fakeChild()
    const ready = waitForManagedInstallerReady(child as never, 'candidate-1', 1000)

    child.emit('exit', 1, null)

    await expect(ready).rejects.toThrow(/exited before readiness/)
  })

  it('rejects a readiness acknowledgement for another candidate', async () => {
    const child = fakeChild()
    const ready = waitForManagedInstallerReady(child as never, 'candidate-1', 1000)

    child.stdio[4]?.write(
      `${JSON.stringify({ event: 'installer-ready', candidate_id: 'candidate-2', pid: child.pid })}\n`
    )

    await expect(ready).rejects.toThrow(/identity does not match/)
  })
})
