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
  it('accepts only a valid inherited descriptor argument', () => {
    expect(parseManagedUpdateHealthRequest(['Hermes', '--managed-update-health-fd', '7'])).toEqual({ fd: 7 })
    expect(parseManagedUpdateHealthRequest(['Hermes', '--managed-update-health-fd', '2'])).toBeNull()
    expect(parseManagedUpdateHealthRequest(['Hermes', '--managed-update-health-fd', '-1'])).toBeNull()
    expect(parseManagedUpdateHealthRequest(['Hermes', '--managed-update-health-fd', '7.5'])).toBeNull()
    expect(parseManagedUpdateHealthRequest(['Hermes', '--managed-update-health-fd', 'not-a-fd'])).toBeNull()
    expect(parseManagedUpdateHealthRequest(['Hermes'])).toBeNull()
  })

  it('publishes readiness through the inherited descriptor and closes it', () => {
    const directory = makeTemporaryDirectory()
    const file = path.join(directory, 'health-pipe-capture.json')
    const fd = fs.openSync(file, 'w+')

    publishManagedUpdateHealth({ fd }, 1234, 5678)

    expect(JSON.parse(fs.readFileSync(file, 'utf8'))).toEqual({ pid: 1234, readyAt: 5678 })
    expect(() => fs.fstatSync(fd)).toThrow()
  })
})