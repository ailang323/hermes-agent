import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

export interface ManagedUpdateConfiguration {
  manifestPath: string
  coordinatorPath: string
  pythonPath: string
  stateRoot: string
  worktree: string
  branch: string
  upstream: string
}

export interface ManagedUpdateEvent {
  event: string
  [key: string]: unknown
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(`Managed update manifest field ${field} must be a non-empty string.`)
  }

  return value
}

function ensureInside(root: string, candidate: string, field: string): void {
  const relative = path.relative(root, candidate)

  if (relative.startsWith('..') || path.isAbsolute(relative)) {
    throw new Error(`Managed update manifest field ${field} must stay inside HERMES_HOME.`)
  }
}

function canonicalExistingPath(value: unknown, field: string): string {
  const candidate = requiredString(value, field)

  if (!path.isAbsolute(candidate)) {
    throw new Error(`Managed update manifest field ${field} must be absolute.`)
  }

  try {
    return fs.realpathSync(candidate)
  } catch (error) {
    throw new Error(`Managed update manifest field ${field} does not exist: ${candidate}`, { cause: error })
  }
}

function absolutePath(value: unknown, field: string): string {
  const candidate = requiredString(value, field)

  if (!path.isAbsolute(candidate)) {
    throw new Error(`Managed update manifest field ${field} must be absolute.`)
  }

  let ancestor = path.resolve(candidate)
  const suffix: string[] = []

  while (!fs.existsSync(ancestor)) {
    const parent = path.dirname(ancestor)

    if (parent === ancestor) {
      throw new Error(`Managed update manifest field ${field} has no existing parent.`)
    }

    suffix.unshift(path.basename(ancestor))
    ancestor = parent
  }

  return path.join(fs.realpathSync(ancestor), ...suffix)
}

export function readManagedUpdateConfiguration(hermesHome: string): ManagedUpdateConfiguration | null {
  const manifestPath = path.join(hermesHome, 'customizations', 'managed-update', 'manifest.json')

  if (!fs.existsSync(manifestPath)) {
    return null
  }

  let raw: unknown

  try {
    raw = JSON.parse(fs.readFileSync(manifestPath, 'utf8'))
  } catch (error) {
    throw new Error(`Managed update manifest is not valid JSON: ${manifestPath}`, { cause: error })
  }

  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('Managed update manifest must contain a JSON object.')
  }

  const manifest = raw as Record<string, unknown>

  if (manifest.schema !== 1 || manifest.mode !== 'managed-patch-stack') {
    throw new Error('Managed update manifest schema or mode is unsupported.')
  }

  const canonicalHome = fs.realpathSync(hermesHome)
  const coordinatorPath = canonicalExistingPath(manifest.coordinator, 'coordinator')
  const pythonPath = canonicalExistingPath(manifest.python, 'python')
  const worktree = canonicalExistingPath(manifest.worktree, 'worktree')
  const stateRoot = absolutePath(manifest.state_root, 'state_root')

  ensureInside(canonicalHome, coordinatorPath, 'coordinator')
  ensureInside(canonicalHome, pythonPath, 'python')
  ensureInside(canonicalHome, worktree, 'worktree')
  ensureInside(canonicalHome, stateRoot, 'state_root')

  return {
    manifestPath: fs.realpathSync(manifestPath),
    coordinatorPath,
    pythonPath,
    stateRoot,
    worktree,
    branch: requiredString(manifest.branch, 'branch'),
    upstream: requiredString(manifest.upstream, 'upstream')
  }
}

function parseEvent(line: string): ManagedUpdateEvent {
  let value: unknown

  try {
    value = JSON.parse(line)
  } catch (error) {
    throw new Error(`Managed update coordinator emitted invalid JSON: ${line.slice(0, 200)}`, { cause: error })
  }

  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Managed update coordinator event must be a JSON object.')
  }

  const event = value as Record<string, unknown>

  if (typeof event.event !== 'string' || event.event.length === 0) {
    throw new Error('Managed update coordinator event is missing an event name.')
  }

  return event as ManagedUpdateEvent
}

function eventString(event: ManagedUpdateEvent, field: string): string {
  const value = event[field]

  if (typeof value !== 'string' || value.length === 0) {
    throw new Error(`Managed update event ${event.event} is missing ${field}.`)
  }

  return value
}

function eventNumber(event: ManagedUpdateEvent, field: string): number {
  const value = event[field]

  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`Managed update event ${event.event} has invalid ${field}.`)
  }

  return value
}

function eventStringArray(event: ManagedUpdateEvent, field: string): string[] {
  const value = event[field]

  if (!Array.isArray(value) || value.some(item => typeof item !== 'string')) {
    throw new Error(`Managed update event ${event.event} has invalid ${field}.`)
  }

  return value
}

function eventRecommendations(
  event: ManagedUpdateEvent,
  field: string
): { feature_id: string; kind: string; commit_subject: string }[] {
  const value = event[field]

  if (!Array.isArray(value)) {
    throw new Error(`Managed update event ${event.event} has invalid ${field}.`)
  }

  return value.map(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      throw new Error(`Managed update event ${event.event} has invalid ${field}.`)
    }

    const row = item as Record<string, unknown>

    if (
      typeof row.feature_id !== 'string' ||
      row.feature_id.length === 0 ||
      typeof row.kind !== 'string' ||
      row.kind.length === 0 ||
      typeof row.commit_subject !== 'string' ||
      row.commit_subject.length === 0
    ) {
      throw new Error(`Managed update event ${event.event} has invalid ${field}.`)
    }

    return {
      feature_id: row.feature_id,
      kind: row.kind,
      commit_subject: row.commit_subject
    }
  })
}

export function mapManagedCheckEvent(event: ManagedUpdateEvent) {
  if (event.event !== 'check-complete') {
    throw new Error(`Expected check-complete, received ${event.event}.`)
  }

  const branch = eventString(event, 'branch')
  const behind = eventNumber(event, 'behind')

  return {
    supported: true,
    managed: true,
    updateAvailable: behind > 0,
    branch,
    currentBranch: branch,
    currentSha: eventString(event, 'original_sha'),
    targetSha: eventString(event, 'upstream_sha'),
    behind,
    dirty: event.clean !== true,
    customCommits: eventNumber(event, 'custom_commits')
  }
}

export function mapManagedApplyEvent(event: ManagedUpdateEvent) {
  if (event.event === 'candidate-decision') {
    return {
      ok: true,
      managed: true,
      managedStage: 'decision' as const,
      candidateId: eventString(event, 'candidate_id'),
      decisionKind: eventString(event, 'status'),
      conflicts: eventStringArray(event, 'conflicts'),
      recommendations: eventRecommendations(event, 'recommendations'),
      report: eventString(event, 'report')
    }
  }

  if (event.event === 'candidate-ready') {
    return {
      ok: true,
      managed: true,
      managedStage: 'confirmation' as const,
      candidateId: eventString(event, 'candidate_id'),
      candidateSha: eventString(event, 'candidate_sha'),
      artifactSha256: eventString(event, 'artifact_sha256'),
      confirmationToken: eventString(event, 'confirmation_token'),
      report: eventString(event, 'report')
    }
  }

  throw new Error(`Unsupported managed apply event: ${event.event}.`)
}

export async function runManagedUpdateCommand(
  configuration: ManagedUpdateConfiguration,
  args: readonly string[],
  onEvent?: (event: ManagedUpdateEvent) => void
): Promise<ManagedUpdateEvent[]> {
  return await new Promise((resolve, reject) => {
    const child = spawn(
      configuration.pythonPath,
      [configuration.coordinatorPath, '--manifest', configuration.manifestPath, ...args],
      {
        env: {
          ...process.env,
          HERMES_HOME: path.resolve(path.dirname(configuration.manifestPath), '..', '..')
        },
        shell: false,
        stdio: ['ignore', 'pipe', 'pipe']
      }
    )

    const events: ManagedUpdateEvent[] = []
    let stdout = ''
    let stderr = ''
    let settled = false

    const fail = (error: Error) => {
      if (settled) {
        return
      }

      settled = true
      child.kill()
      reject(error)
    }

    const consumeLines = (flush: boolean) => {
      const lines = stdout.split('\n')
      stdout = flush ? '' : (lines.pop() ?? '')

      if (flush && lines.at(-1) !== '') {
        lines.push(stdout)
      }

      for (const rawLine of lines) {
        const line = rawLine.trim()

        if (!line) {
          continue
        }

        try {
          const event = parseEvent(line)
          events.push(event)
          onEvent?.(event)
        } catch (error) {
          fail(error instanceof Error ? error : new Error(String(error)))
        }
      }
    }

    child.stdout.setEncoding('utf8')
    child.stdout.on('data', chunk => {
      stdout += chunk

      if (stdout.length > 1024 * 1024) {
        fail(new Error('Managed update coordinator emitted an oversized JSONL record.'))

        return
      }

      consumeLines(false)
    })
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', chunk => {
      stderr = `${stderr}${chunk}`.slice(-64 * 1024)
    })
    child.on('error', error => fail(error))
    child.on('close', code => {
      if (settled) {
        return
      }

      try {
        if (stdout.trim()) {
          const event = parseEvent(stdout.trim())
          events.push(event)
          onEvent?.(event)
        }
      } catch (error) {
        fail(error instanceof Error ? error : new Error(String(error)))

        return
      }

      if (code !== 0) {
        const coordinatorError = events.findLast(event => event.event === 'error')

        const message =
          (typeof coordinatorError?.message === 'string' && coordinatorError.message) ||
          stderr.trim() ||
          `Managed update coordinator exited with code ${String(code)}.`

        fail(new Error(message))

        return
      }

      if (events.length === 0) {
        fail(new Error('Managed update coordinator exited without emitting a result.'))

        return
      }

      settled = true
      resolve(events)
    })
  })
}

export type ManagedUpdateRunner = typeof runManagedUpdateCommand

function finalManagedEvent(events: readonly ManagedUpdateEvent[], names: readonly string[]): ManagedUpdateEvent {
  const event = events.findLast(item => names.includes(item.event))

  if (!event) {
    throw new Error(`Managed update coordinator did not emit ${names.join(' or ')}.`)
  }

  return event
}

export async function checkManagedUpdates(
  configuration: ManagedUpdateConfiguration,
  onEvent?: (event: ManagedUpdateEvent) => void,
  runner: ManagedUpdateRunner = runManagedUpdateCommand
) {
  const events = await runner(configuration, ['check'], onEvent)

  return mapManagedCheckEvent(finalManagedEvent(events, ['check-complete']))
}

export async function recoverManagedUpdates(
  configuration: ManagedUpdateConfiguration,
  onEvent?: (event: ManagedUpdateEvent) => void,
  runner: ManagedUpdateRunner = runManagedUpdateCommand
) {
  const events = await runner(
    configuration,
    ['recover', '--state-root', configuration.stateRoot],
    onEvent
  )

  const event = finalManagedEvent(events, ['recovery-complete'])
  const recovered = event.recovered
  const reports = event.reports

  if (
    !Number.isInteger(recovered) ||
    (recovered as number) < 0 ||
    !Array.isArray(reports) ||
    reports.some(report => typeof report !== 'string') ||
    reports.length !== recovered
  ) {
    throw new Error('Managed update recovery result is invalid.')
  }

  return { recovered: recovered as number, reports: reports as string[] }
}

export async function acceptManagedUpdateReview(
  configuration: ManagedUpdateConfiguration,
  candidateId: string,
  onEvent?: (event: ManagedUpdateEvent) => void,
  runner: ManagedUpdateRunner = runManagedUpdateCommand
) {
  assertCandidateId(candidateId)

  const events = await runner(
    configuration,
    ['accept-review', '--state-root', configuration.stateRoot, '--candidate-id', candidateId],
    onEvent
  )

  return mapManagedApplyEvent(finalManagedEvent(events, ['candidate-ready']))
}

function assertCandidateId(candidateId: string): void {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(candidateId)) {
    throw new Error('Managed update candidate ID is invalid.')
  }
}

export async function cancelManagedUpdate(
  configuration: ManagedUpdateConfiguration,
  candidateId: string,
  onEvent?: (event: ManagedUpdateEvent) => void,
  runner: ManagedUpdateRunner = runManagedUpdateCommand
) {
  assertCandidateId(candidateId)

  const events = await runner(
    configuration,
    ['cancel', '--state-root', configuration.stateRoot, '--candidate-id', candidateId],
    onEvent
  )

  const event = finalManagedEvent(events, ['candidate-cancelled'])

  if (eventString(event, 'candidate_id') !== candidateId || eventString(event, 'status') !== 'cancelled') {
    throw new Error('Managed update cancellation result does not match the requested candidate.')
  }

  return {
    ok: false,
    managed: true,
    cancelled: true,
    candidateId,
    report: eventString(event, 'report')
  }
}

export async function prepareManagedUpdate(
  configuration: ManagedUpdateConfiguration,
  candidateId: string,
  onEvent?: (event: ManagedUpdateEvent) => void,
  runner: ManagedUpdateRunner = runManagedUpdateCommand
) {
  assertCandidateId(candidateId)

  const events = await runner(
    configuration,
    ['prepare', '--state-root', configuration.stateRoot, '--candidate-id', candidateId],
    onEvent
  )

  return mapManagedApplyEvent(finalManagedEvent(events, ['candidate-decision', 'candidate-ready']))
}
