import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import { parseManagedUpdateHealthRequest, publishManagedUpdateHealth } from './managed-update-health'

const temporaryDirectories: string[] = []

function makeTemporaryDirectory(): string {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'managed-update-health-test-'))
  temporaryDirectories.push(directory)

  return directory
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    fs.rmSync(directory, { recursive: true, force: true })
  }
})

describe('managed update health protocol', () => {
  it('accepts only a constrained health file and 64-character token in the allowed temp directory', () => {
    const directory = makeTemporaryDirectory()
    const file = path.join(directory, 'hermes-managed-update-health-candidate-1.json')
    const token = 'a'.repeat(64)

    expect(
      parseManagedUpdateHealthRequest(
        ['Hermes', '--managed-update-health-file', file, '--managed-update-health-token', token],
        directory
      )
    ).toEqual({ file, token })
    expect(
      parseManagedUpdateHealthRequest(
        ['Hermes', '--managed-update-health-file', path.join(directory, 'arbitrary.json'), '--managed-update-health-token', token],
        directory
      )
    ).toBeNull()
    expect(
      parseManagedUpdateHealthRequest(
        ['Hermes', '--managed-update-health-file', file, '--managed-update-health-token', 'short'],
        directory
      )
    ).toBeNull()
    expect(
      parseManagedUpdateHealthRequest(
        ['Hermes', '--managed-update-health-file', path.join(path.dirname(directory), path.basename(file)), '--managed-update-health-token', token],
        directory
      )
    ).toBeNull()
  })

  it('atomically publishes the nonce-bound ready record without overwriting an existing record', () => {
    const directory = makeTemporaryDirectory()

    const request = {
      file: path.join(directory, 'hermes-managed-update-health-candidate-2.json'),
      token: 'b'.repeat(64)
    }

    publishManagedUpdateHealth(request, 1234, 5678)
    expect(JSON.parse(fs.readFileSync(request.file, 'utf8'))).toEqual({
      token: request.token,
      pid: 1234,
      readyAt: 5678
    })
    expect(() => publishManagedUpdateHealth(request, 9999, 9999)).toThrow()
    expect(JSON.parse(fs.readFileSync(request.file, 'utf8'))).toEqual({
      token: request.token,
      pid: 1234,
      readyAt: 5678
    })
  })
})
