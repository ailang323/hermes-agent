import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Dialog, DialogContent } from '@/components/ui/dialog'
import type { UpdateApplyState } from '@/store/updates'

import {
  managedCancelRequest,
  ManagedConfirmationView,
  ManagedDecisionView,
  managedInstallRequest,
  managedResumeConflictRequest
} from './updates-overlay'

afterEach(() => cleanup())

function state(overrides: Partial<UpdateApplyState>): UpdateApplyState {
  return {
    applying: false,
    stage: 'managedDecision',
    message: '',
    percent: null,
    error: null,
    command: null,
    managed: null,
    managedRequest: null,
    log: [],
    ...overrides
  }
}

describe('managed update decision views', () => {
  it('creates renderer requests without approval capabilities', () => {
    expect(managedInstallRequest('candidate-2')).toEqual({
      managedAction: 'install',
      candidateId: 'candidate-2'
    })
    expect(managedCancelRequest('candidate-2')).toEqual({
      managedAction: 'cancel',
      candidateId: 'candidate-2'
    })
    expect(managedResumeConflictRequest('candidate-2')).toEqual({
      managedAction: 'resume-conflict',
      candidateId: 'candidate-2'
    })
  })

  it('shows conflict files without exposing an install action', () => {
    const onResumeConflict = vi.fn()

    const view = render(
      <Dialog open>
        <DialogContent>
          <ManagedDecisionView
            apply={state({
              managed: {
                ok: true,
                managed: true,
                managedStage: 'decision',
                candidateId: 'candidate-1',
                decisionKind: 'conflict',
                conflicts: ['apps/desktop/electron/main.ts'],
                recommendations: [],
                report: '/tmp/report.json',
                worktree: '/tmp/candidate/worktree'
              }
            })}
            onCancel={vi.fn()}
            onResumeConflict={onResumeConflict}
          />
        </DialogContent>
      </Dialog>
    )

    expect(view.getByText('apps/desktop/electron/main.ts')).toBeTruthy()
    expect(view.getByText('/tmp/candidate/worktree')).toBeTruthy()
    expect(view.queryByRole('button', { name: /install/i })).toBeNull()
    fireEvent.click(view.getByRole('button', { name: /resolved.*continue verification/i }))
    expect(onResumeConflict).toHaveBeenCalledTimes(1)
  })

  it('offers the explicit upstream-equivalent approval only for review candidates', () => {
    const onAcceptReview = vi.fn()

    const view = render(
      <Dialog open>
        <DialogContent>
          <ManagedDecisionView
            apply={state({
              managed: {
                ok: true,
                managed: true,
                managedStage: 'decision',
                candidateId: 'candidate-review',
                decisionKind: 'review',
                conflicts: [],
                recommendations: [
                  {
                    feature_id: 'local-feature',
                    kind: 'upstream-equivalent',
                    commit_subject: 'feat: local feature'
                  }
                ],
                report: '/tmp/report.json'
              }
            })}
            onAcceptReview={onAcceptReview}
            onCancel={vi.fn()}
          />
        </DialogContent>
      </Dialog>
    )

    expect(view.getByText('Use the upstream implementation for this feature.')).toBeTruthy()
    expect(view.getByText('The rebased candidate no longer needs the local patch: feat: local feature')).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: /use upstream implementation and continue/i }))
    expect(onAcceptReview).toHaveBeenCalledTimes(1)
  })

  it('shows exact commit and artifact hash before invoking final confirmation', () => {
    const onConfirm = vi.fn()

    const view = render(
      <Dialog open>
        <DialogContent>
          <ManagedConfirmationView
            apply={state({
              stage: 'managedConfirmation',
              managed: {
                ok: true,
                managed: true,
                managedStage: 'confirmation',
                candidateId: 'candidate-2',
                candidateSha: 'abcdef1234567890',
                artifactSha256: 'a'.repeat(64),
                report: '/tmp/verification.json'
              }
            })}
            onCancel={vi.fn()}
            onConfirm={onConfirm}
          />
        </DialogContent>
      </Dialog>
    )

    expect(view.getByText('abcdef1234567890')).toBeTruthy()
    expect(view.getByText('a'.repeat(64))).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: /install verified update/i }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })
})
