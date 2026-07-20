import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  acceptManagedUpdateReview,
  cancelManagedUpdate,
  checkManagedUpdates,
  type ManagedUpdateConfiguration,
  mapManagedApplyEvent,
  mapManagedCheckEvent,
  prepareManagedUpdate,
  readManagedUpdateConfiguration,
  recoverManagedUpdates,
  resumeManagedUpdateConflict,
  runManagedUpdateCommand
} from './managed-update'

function temporaryDirectory(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-managed-update-'))
}

test('mapManagedCheckEvent preserves managed branch distance and dirty state', () => {
  assert.deepEqual(
    mapManagedCheckEvent({
      event: 'check-complete',
      branch: 'feat/longer-stable-v2',
      original_sha: '1111111',
      upstream_sha: '2222222',
      ahead: 8,
      behind: 3,
      clean: false,
      custom_commits: 8
    }),
    {
      supported: true,
      managed: true,
      updateAvailable: true,
      branch: 'feat/longer-stable-v2',
      currentBranch: 'feat/longer-stable-v2',
      currentSha: '1111111',
      targetSha: '2222222',
      behind: 3,
      dirty: true,
      customCommits: 8
    }
  )
})

test('mapManagedApplyEvent returns explicit decision and confirmation states', () => {
  assert.deepEqual(
    mapManagedApplyEvent({
      event: 'candidate-decision',
      candidate_id: 'candidate-1',
      status: 'conflict',
      conflicts: ['apps/desktop/electron/main.ts'],
      recommendations: [
        {
          feature_id: 'plugin-locale',
          kind: 'upstream-equivalent',
          commit_subject: 'feat(desktop): expose active locale'
        }
      ],
      report: '/tmp/report.json',
      worktree: '/tmp/candidate/worktree'
    }),
    {
      ok: true,
      managed: true,
      managedStage: 'decision',
      candidateId: 'candidate-1',
      decisionKind: 'conflict',
      conflicts: ['apps/desktop/electron/main.ts'],
      recommendations: [
        {
          feature_id: 'plugin-locale',
          kind: 'upstream-equivalent',
          commit_subject: 'feat(desktop): expose active locale'
        }
      ],
      report: '/tmp/report.json',
      worktree: '/tmp/candidate/worktree'
    }
  )
  assert.deepEqual(
    mapManagedApplyEvent({
      event: 'candidate-ready',
      candidate_id: 'candidate-2',
      status: 'verified',
      candidate_sha: '3333333',
      artifact_sha256: 'a'.repeat(64),
      verification_report_sha256: 'c'.repeat(64),
      confirmation_token: 'b'.repeat(64),
      report: '/tmp/verification.json'
    }),
    {
      ok: true,
      managed: true,
      managedStage: 'confirmation',
      candidateId: 'candidate-2',
      candidateSha: '3333333',
      artifactSha256: 'a'.repeat(64),
      verificationReportSha256: 'c'.repeat(64),
      confirmationToken: 'b'.repeat(64),
      report: '/tmp/verification.json'
    }
  )
  assert.throws(
    () =>
      mapManagedApplyEvent({
        event: 'candidate-decision',
        candidate_id: 'candidate-invalid',
        status: 'review',
        conflicts: [],
        recommendations: [{ feature_id: 'plugin-locale' }],
        report: '/tmp/report.json'
      }),
    /invalid recommendations/
  )
})

test('managed service routes check and prepare with fixed coordinator arguments', async () => {
  const configuration: ManagedUpdateConfiguration = {
    manifestPath: '/tmp/manifest.json',
    coordinatorPath: '/tmp/coordinator.py',
    pythonPath: '/tmp/python',
    stateRoot: '/tmp/state',
    worktree: '/tmp/worktree',
    branch: 'feat/longer-stable-v2',
    upstream: 'upstream/main'
  }

  const seen: string[][] = []

  const runner: typeof runManagedUpdateCommand = async (_configuration, args, onEvent) => {
    seen.push([...args])

    const event =
      args[0] === 'check'
        ? {
            event: 'check-complete',
            branch: 'feat/longer-stable-v2',
            original_sha: '1111111',
            upstream_sha: '2222222',
            ahead: 8,
            behind: 2,
            clean: true,
            custom_commits: 8
          }
        : args[0] === 'recover'
          ? {
              event: 'recovery-complete',
              recovered: 1,
              reports: ['/tmp/recovery.json']
            }
          : args[0] === 'cancel'
          ? {
              event: 'candidate-cancelled',
              candidate_id: 'candidate-7',
              status: 'cancelled',
              report: '/tmp/cancellation.json'
            }
          : {
              event: 'candidate-ready',
            candidate_id: 'candidate-7',
            status: 'verified',
            candidate_sha: '3333333',
            artifact_sha256: 'a'.repeat(64),
            verification_report_sha256: 'c'.repeat(64),
            confirmation_token: 'b'.repeat(64),
            report: '/tmp/verification.json'
          }

    onEvent?.(event)

    return [event]
  }

  const checked = await checkManagedUpdates(configuration, undefined, runner)
  const recovery = await recoverManagedUpdates(configuration, undefined, runner)
  const prepared = await prepareManagedUpdate(configuration, 'candidate-7', undefined, runner)
  const accepted = await acceptManagedUpdateReview(configuration, 'candidate-7', undefined, runner)
  const resumed = await resumeManagedUpdateConflict(configuration, 'candidate-7', undefined, runner)
  const cancelled = await cancelManagedUpdate(configuration, 'candidate-7', undefined, runner)

  assert.equal(checked.behind, 2)
  assert.equal(recovery.recovered, 1)
  assert.equal(prepared.managedStage, 'confirmation')
  assert.equal(accepted.managedStage, 'confirmation')
  assert.equal(resumed.managedStage, 'confirmation')
  assert.equal(cancelled.cancelled, true)
  assert.deepEqual(seen, [
    ['check'],
    ['recover', '--state-root', configuration.stateRoot],
    ['prepare', '--state-root', configuration.stateRoot, '--candidate-id', 'candidate-7'],
    ['accept-review', '--state-root', configuration.stateRoot, '--candidate-id', 'candidate-7'],
    ['resume-conflict', '--state-root', configuration.stateRoot, '--candidate-id', 'candidate-7'],
    ['cancel', '--state-root', configuration.stateRoot, '--candidate-id', 'candidate-7']
  ])
})

test('readManagedUpdateConfiguration validates and normalizes managed paths', () => {
  const root = temporaryDirectory()

  try {
    const hermesHome = path.join(root, '.hermes')
    const manifestDirectory = path.join(hermesHome, 'customizations', 'managed-update')
    const coordinatorPath = path.join(hermesHome, 'bin', 'managed_update_coordinator.py')
    const pythonPath = path.join(hermesHome, 'hermes-agent', '.venv', 'bin', 'python')
    const worktree = path.join(hermesHome, 'worktrees', 'custom')
    const stateRoot = path.join(hermesHome, 'update-staging')
    const installedApp = path.join(root, 'Applications', 'Hermes.app')
    const manifestPath = path.join(manifestDirectory, 'manifest.json')

    for (const directory of [manifestDirectory, path.dirname(coordinatorPath), path.dirname(pythonPath), worktree]) {
      fs.mkdirSync(directory, { recursive: true })
    }

    fs.writeFileSync(coordinatorPath, '# coordinator\n')
    fs.writeFileSync(pythonPath, '# python\n')
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        schema: 1,
        mode: 'managed-patch-stack',
        coordinator: coordinatorPath,
        python: pythonPath,
        state_root: stateRoot,
        worktree,
        branch: 'feat/longer-stable-v2',
        upstream: 'upstream/main',
        installed_app: installedApp,
        features: []
      })
    )

    const configuration = readManagedUpdateConfiguration(hermesHome)

    assert.ok(configuration)
    const canonicalHome = fs.realpathSync(hermesHome)
    assert.equal(configuration.manifestPath, fs.realpathSync(manifestPath))
    assert.equal(configuration.coordinatorPath, fs.realpathSync(coordinatorPath))
    assert.equal(configuration.pythonPath, fs.realpathSync(pythonPath))
    assert.equal(configuration.stateRoot, path.join(canonicalHome, 'update-staging'))
    assert.equal(configuration.worktree, fs.realpathSync(worktree))
    assert.equal(configuration.branch, 'feat/longer-stable-v2')
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('runManagedUpdateCommand parses JSONL events from a real child process', async () => {
  const root = temporaryDirectory()

  try {
    const script = path.join(root, 'coordinator.mjs')
    const manifestPath = path.join(root, 'manifest.json')
    fs.writeFileSync(manifestPath, '{}')
    fs.writeFileSync(
      script,
      [
        "console.log(JSON.stringify({ event: 'phase', stage: 'fetch' }))",
        "console.log(JSON.stringify({ event: 'check-complete', behind: 3, clean: true }))"
      ].join('\n')
    )

    const configuration: ManagedUpdateConfiguration = {
      manifestPath,
      coordinatorPath: script,
      pythonPath: process.execPath,
      stateRoot: path.join(root, 'state'),
      worktree: root,
      branch: 'feat/longer-stable-v2',
      upstream: 'upstream/main'
    }

    const streamed: string[] = []

    const events = await runManagedUpdateCommand(configuration, ['check', '--no-fetch'], event => {
      streamed.push(event.event)
    })

    assert.deepEqual(streamed, ['phase', 'check-complete'])
    assert.equal(events.at(-1)?.event, 'check-complete')
    assert.equal(events.at(-1)?.behind, 3)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
