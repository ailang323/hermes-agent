import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { BrandMark } from '@/components/brand-mark'
import { Button } from '@/components/ui/button'
import { writeClipboardText } from '@/components/ui/copy-button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  preventCloseButtonAutoFocus
} from '@/components/ui/dialog'
import { ErrorIcon, ErrorState } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import type {
  DesktopUpdateApplyOptions,
  DesktopUpdateCommit,
  DesktopUpdateStage,
  DesktopUpdateStatus
} from '@/global'
import { useI18n } from '@/i18n'
import { buildCommitChangelog, type CommitGroup } from '@/lib/commit-changelog'
import { AlertCircle, Check, Copy, Terminal } from '@/lib/icons'
import { resolveUpdateCopy, type UpdateTarget } from '@/lib/update-copy'
import { cn } from '@/lib/utils'
import {
  $backendUpdateApply,
  $backendUpdateChecking,
  $backendUpdateStatus,
  $updateApply,
  $updateChecking,
  $updateOverlayOpen,
  $updateOverlayTarget,
  $updateStatus,
  applyBackendUpdate,
  applyUpdates,
  checkBackendUpdates,
  checkUpdates,
  resetUpdateApplyState,
  setUpdateOverlayOpen,
  type UpdateApplyState
} from '@/store/updates'

function totalItems(groups: readonly CommitGroup[]) {
  return groups.reduce((sum, g) => sum + g.items.length, 0)
}

export function managedInstallRequest(candidateId: string): DesktopUpdateApplyOptions {
  return { managedAction: 'install', candidateId }
}

export function managedCancelRequest(candidateId: string): DesktopUpdateApplyOptions {
  return { managedAction: 'cancel', candidateId }
}

export function managedResumeConflictRequest(candidateId: string): DesktopUpdateApplyOptions {
  return { managedAction: 'resume-conflict', candidateId }
}

export function UpdatesOverlay() {
  const open = useStore($updateOverlayOpen)
  const target = useStore($updateOverlayTarget)

  const clientStatus = useStore($updateStatus)
  const clientChecking = useStore($updateChecking)
  const clientApply = useStore($updateApply)
  const backendStatus = useStore($backendUpdateStatus)
  const backendChecking = useStore($backendUpdateChecking)
  const backendApply = useStore($backendUpdateApply)

  const isBackend = target === 'backend'
  const status = isBackend ? backendStatus : clientStatus
  const checking = isBackend ? backendChecking : clientChecking
  const apply = isBackend ? backendApply : clientApply
  const check = isBackend ? checkBackendUpdates : checkUpdates
  const install = isBackend ? applyBackendUpdate : applyUpdates

  useEffect(() => {
    if (open && !status && !checking) {
      void check()
    }
  }, [check, checking, open, status])

  const behind = status?.behind ?? 0
  const updateAvailable = status?.updateAvailable || behind > 0

  const phase: 'idle' | 'applying' | 'managedDecision' | 'managedConfirmation' | 'manual' | 'guiSkew' | 'error' =
    apply.stage === 'managedDecision'
      ? 'managedDecision'
      : apply.stage === 'managedConfirmation'
        ? 'managedConfirmation'
        : apply.stage === 'manual'
          ? 'manual'
          : apply.stage === 'guiSkew'
            ? 'guiSkew'
            : apply.applying || apply.stage === 'restart'
              ? 'applying'
              : apply.stage === 'error'
                ? 'error'
                : 'idle'

  const handleClose = (next: boolean) => {
    if (phase === 'applying') {
      return
    }

    const candidateId = apply.managed?.candidateId

    if (
      !next &&
      candidateId &&
      (apply.stage === 'managedDecision' || apply.stage === 'managedConfirmation')
    ) {
      void applyUpdates(managedCancelRequest(candidateId))

      return
    }

    setUpdateOverlayOpen(next)

    if (
      !next &&
      (apply.stage === 'error' ||
        apply.stage === 'restart' ||
        apply.stage === 'managedDecision' ||
        apply.stage === 'managedConfirmation' ||
        apply.stage === 'manual' ||
        apply.stage === 'guiSkew')
    ) {
      resetUpdateApplyState()
    }
  }

  const handleInstall = () => {
    void install()
  }

  const handleRetry = () => {
    if (!isBackend && apply.managedRequest) {
      void applyUpdates(apply.managedRequest)

      return
    }

    void install()
  }

  const handleManagedAcceptReview = () => {
    const candidateId = apply.managed?.candidateId

    if (!candidateId) {
      return
    }

    void applyUpdates({ managedAction: 'accept-review', candidateId })
  }

  const handleManagedResumeConflict = () => {
    const candidateId = apply.managed?.candidateId

    if (!candidateId) {
      return
    }

    void applyUpdates(managedResumeConflictRequest(candidateId))
  }

  const handleManagedConfirm = () => {
    const candidateId = apply.managed?.candidateId

    if (!candidateId) {
      return
    }

    void applyUpdates(managedInstallRequest(candidateId))
  }

  return (
    <Dialog onOpenChange={handleClose} open={open}>
      {/* This dialog has no inputs, so Radix's default autofocus would land on
          the close button and trigger its tooltip immediately on open. */}
      <DialogContent
        className="max-h-[85vh] max-w-sm overflow-y-auto p-0 gap-0"
        onOpenAutoFocus={preventCloseButtonAutoFocus}
        showCloseButton={phase !== 'applying'}
      >
        {phase === 'applying' && <ApplyingView apply={apply} isBackend={isBackend} />}

        {phase === 'managedDecision' && (
          <ManagedDecisionView
            apply={apply}
            onAcceptReview={handleManagedAcceptReview}
            onCancel={() => handleClose(false)}
            onResumeConflict={handleManagedResumeConflict}
          />
        )}

        {phase === 'managedConfirmation' && (
          <ManagedConfirmationView
            apply={apply}
            onCancel={() => handleClose(false)}
            onConfirm={handleManagedConfirm}
          />
        )}

        {phase === 'manual' && (
          <ManualView command={apply.command ?? null} message={apply.message} onDone={() => handleClose(false)} />
        )}

        {phase === 'guiSkew' && <GuiSkewView message={apply.message} onDone={() => handleClose(false)} />}

        {phase === 'error' && (
          <ErrorView message={apply.message} onDismiss={() => handleClose(false)} onRetry={handleRetry} />
        )}

        {phase === 'idle' && (
          <IdleView
            behind={behind}
            checking={checking}
            commits={status?.commits ?? []}
            onInstall={handleInstall}
            onLater={() => handleClose(false)}
            onRetryCheck={() => void check()}
            status={status}
            target={target}
            updateAvailable={updateAvailable}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function IdleView({
  behind,
  checking,
  commits,
  onInstall,
  onLater,
  onRetryCheck,
  status,
  target,
  updateAvailable
}: {
  behind: number
  checking: boolean
  commits: readonly DesktopUpdateCommit[]
  onInstall: () => void
  onLater: () => void
  onRetryCheck: () => void
  status: DesktopUpdateStatus | null
  target: UpdateTarget
  updateAvailable: boolean
}) {
  const { t } = useI18n()
  const u = t.updates

  if (!status && checking) {
    return (
      <CenteredStatus
        icon={<Loader className="size-12" label={u.checking} type="lemniscate-bloom" />}
        title={u.checking}
      />
    )
  }

  if (!status) {
    return (
      <CenteredStatus
        action={
          <Button onClick={onRetryCheck} size="sm">
            {u.tryAgain}
          </Button>
        }
        icon={<ErrorIcon />}
        title={u.checkFailedTitle}
      />
    )
  }

  if (!status.supported) {
    return (
      <CenteredStatus
        body={status.message ?? u.unsupportedMessage}
        icon={<AlertCircle className="size-6 text-muted-foreground" />}
        title={u.notAvailableTitle}
      />
    )
  }

  if (status.error) {
    return (
      <CenteredStatus
        action={
          <Button disabled={checking} onClick={onRetryCheck} size="sm">
            {u.tryAgain}
          </Button>
        }
        body={u.connectionRetry}
        icon={<ErrorIcon />}
        title={u.checkFailedTitle}
      />
    )
  }

  if (!updateAvailable) {
    return (
      <CenteredStatus
        body={target === 'backend' ? u.latestBodyBackend : u.latestBody}
        icon={<BrandMark className="size-12" />}
        title={u.allSetTitle}
      />
    )
  }

  const groups = buildCommitChangelog(commits)
  const shownItems = totalItems(groups)
  const remaining = Math.max(0, behind - shownItems)

  // Name what's being updated. In remote mode the overlay acts on the connected
  // backend, not the local client — say so. When there are no commit rows to
  // show (e.g. pip/non-git backend), degrade to honest "no release notes" copy
  // instead of generic filler.
  const { title, body } = resolveUpdateCopy({ target, shownItems, copy: u })

  return (
    <div className="grid gap-5 px-6 pb-6 pt-7 pr-8">
      <div className="flex flex-col items-center gap-3 text-center">
        <BrandMark className="size-16" />

        <DialogTitle className="text-center text-xl">{title}</DialogTitle>
        <DialogDescription className="text-center text-sm">{body}</DialogDescription>
      </div>

      <div className="grid gap-3">
        {groups.map(group => (
          <div key={group.id}>
            <p className="text-[0.625rem] font-semibold uppercase tracking-wide text-muted-foreground">{group.label}</p>
            <ul className="mt-1.5 grid gap-1.5 text-xs text-foreground">
              {group.items.map(item => (
                <li className="flex items-start gap-2" key={item}>
                  <span aria-hidden className="mt-1.5 inline-block size-1 shrink-0 rounded-full bg-primary" />
                  <span className="leading-snug">{item}</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      <div className="grid gap-2">
        <Button className="font-semibold" onClick={onInstall} size="lg">
          {u.updateNow}
        </Button>
        <Button className="font-medium" onClick={onLater} type="button" variant="text">
          {u.maybeLater}
        </Button>
      </div>

      {remaining > 0 && <p className="text-center text-xs text-muted-foreground">{u.moreChanges(remaining)}</p>}
    </div>
  )
}

export function ManagedDecisionView({
  apply,
  onAcceptReview,
  onCancel,
  onResumeConflict
}: {
  apply: UpdateApplyState
  onAcceptReview?: () => void
  onCancel: () => void
  onResumeConflict?: () => void
}) {
  const { t } = useI18n()
  const u = t.updates
  const managed = apply.managed
  const conflicts = managed?.conflicts ?? []
  const recommendations = managed?.recommendations ?? []
  const isConflict = managed?.decisionKind === 'conflict'

  return (
    <div className="grid gap-5 px-6 pb-6 pt-7 pr-8">
      <div className="flex flex-col items-center gap-3 text-center">
        <AlertCircle className="size-9 text-amber-500" />
        <DialogTitle className="text-center text-xl">{u.managedDecisionTitle}</DialogTitle>
        <DialogDescription className="text-center text-sm">
          {isConflict ? u.managedConflictBody : u.managedReviewBody}
        </DialogDescription>
      </div>

      {conflicts.length > 0 && (
        <section className="grid gap-2">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {u.managedConflictFiles}
          </p>
          <ul className="grid gap-1 rounded-md border border-border/70 bg-muted/35 p-3 font-mono text-xs">
            {conflicts.map(file => (
              <li className="break-all" key={file}>
                {file}
              </li>
            ))}
          </ul>
        </section>
      )}

      {recommendations.length > 0 && (
        <section className="grid gap-2">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {u.managedRecommendations}
          </p>
          <ul className="grid gap-2 rounded-md border border-border/70 bg-muted/35 p-3 text-xs">
            {recommendations.map(item => (
              <li className="grid gap-1" key={item.feature_id}>
                <span className="font-semibold">{item.feature_id}</span>
                <span>
                  {item.kind === 'upstream-equivalent' ? u.managedUpstreamEquivalentRecommendation : item.kind}
                </span>
                <span className="text-muted-foreground">
                  {item.kind === 'upstream-equivalent'
                    ? `${u.managedUpstreamEquivalentReason} ${item.commit_subject}`
                    : item.commit_subject}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {isConflict && managed?.worktree && (
        <div className="grid gap-1 text-xs">
          <span className="font-semibold text-muted-foreground">{u.managedWorktree}</span>
          <code className="break-all rounded-md border border-border/70 bg-muted/35 px-3 py-2">
            {managed.worktree}
          </code>
        </div>
      )}

      {managed?.report && (
        <div className="grid gap-1 text-xs">
          <span className="font-semibold text-muted-foreground">{u.managedReport}</span>
          <code className="break-all rounded-md border border-border/70 bg-muted/35 px-3 py-2">{managed.report}</code>
        </div>
      )}

      <div className="grid gap-2">
        {isConflict && onResumeConflict && (
          <Button className="font-semibold" onClick={onResumeConflict} size="lg">
            {u.managedResumeConflict}
          </Button>
        )}
        {!isConflict && recommendations.length > 0 && onAcceptReview && (
          <Button className="font-semibold" onClick={onAcceptReview} size="lg">
            {u.managedAcceptRecommendations}
          </Button>
        )}
        <Button className="font-semibold" onClick={onCancel} size="lg" variant="secondary">
          {u.managedCancel}
        </Button>
      </div>
    </div>
  )
}

export function ManagedConfirmationView({
  apply,
  onCancel,
  onConfirm
}: {
  apply: UpdateApplyState
  onCancel: () => void
  onConfirm: () => void
}) {
  const { t } = useI18n()
  const u = t.updates
  const managed = apply.managed

  return (
    <div className="grid gap-5 px-6 pb-6 pt-7 pr-8">
      <div className="flex flex-col items-center gap-3 text-center">
        <Check className="size-9 text-primary" />
        <DialogTitle className="text-center text-xl">{u.managedConfirmationTitle}</DialogTitle>
        <DialogDescription className="text-center text-sm">{u.managedConfirmationBody}</DialogDescription>
      </div>

      <dl className="grid gap-3 rounded-md border border-border/70 bg-muted/35 p-3 text-xs">
        <div className="grid gap-1">
          <dt className="font-semibold text-muted-foreground">{u.managedCandidateCommit}</dt>
          <dd className="break-all font-mono text-foreground">{managed?.candidateSha ?? '—'}</dd>
        </div>
        <div className="grid gap-1">
          <dt className="font-semibold text-muted-foreground">{u.managedArtifactHash}</dt>
          <dd className="break-all font-mono text-foreground">{managed?.artifactSha256 ?? '—'}</dd>
        </div>
      </dl>

      <div className="grid gap-2">
        <Button className="font-semibold" onClick={onConfirm} size="lg">
          {u.managedInstallVerified}
        </Button>
        <Button className="font-medium" onClick={onCancel} type="button" variant="text">
          {u.managedCancel}
        </Button>
      </div>
    </div>
  )
}

function ManualView({ command, message, onDone }: { command: string | null; message?: string; onDone: () => void }) {
  const { t } = useI18n()
  const u = t.updates
  const [copied, setCopied] = useState(false)

  const handleCopy = () => {
    if (!command) {
      return
    }

    void writeClipboardText(command).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1800)
    })
  }

  // No command (e.g. the Linux sandbox-blocked relaunch): render the explanatory
  // message + a Done button, not a copy-a-command box.
  if (!command) {
    return (
      <div className="grid gap-5 px-6 pb-6 pt-7 pr-8">
        <div className="flex flex-col items-center gap-3 text-center">
          <Terminal className="size-8 text-primary" />

          <DialogTitle className="text-center text-xl">{u.manualTitle}</DialogTitle>
          <DialogDescription className="text-center text-sm">{message || u.manualPickedUp}</DialogDescription>
        </div>

        <Button className="font-semibold" onClick={onDone} size="lg" variant="secondary">
          {u.done}
        </Button>
      </div>
    )
  }

  return (
    <div className="grid gap-5 px-6 pb-6 pt-7 pr-8">
      <div className="flex flex-col items-center gap-3 text-center">
        <Terminal className="size-8 text-primary" />

        <DialogTitle className="text-center text-xl">{u.manualTitle}</DialogTitle>
        <DialogDescription className="text-center text-sm">{u.manualBody}</DialogDescription>
      </div>

      <button
        className={cn(
          'group flex w-full items-center justify-between gap-3 rounded-md border px-4 py-3 text-left transition-colors',
          copied ? 'border-primary/50' : 'border-(--stroke-nous) hover:border-(--ui-stroke-secondary)'
        )}
        onClick={handleCopy}
        type="button"
      >
        <code className="min-w-0 flex-1 truncate select-all font-mono text-sm text-foreground">
          <span className="select-none text-muted-foreground">$ </span>
          {command}
        </code>
        <span
          className={cn(
            'flex shrink-0 items-center gap-1 text-xs font-medium transition-colors',
            copied ? 'text-primary' : 'text-muted-foreground group-hover:text-foreground'
          )}
        >
          {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          {copied ? u.copied : u.copy}
        </span>
      </button>

      <p className="text-center text-xs text-muted-foreground">{u.manualPickedUp}</p>

      <Button className="font-semibold" onClick={onDone} size="lg" variant="secondary">
        {u.done}
      </Button>
    </div>
  )
}

// Linux GUI/backend skew (#45205): backend updated, but the running desktop app
// package (AppImage/.deb/.rpm) was NOT changed. Closeable terminal state that
// tells the user to update/reinstall the desktop app — never claims the GUI was
// updated.
function GuiSkewView({ message, onDone }: { message?: string; onDone: () => void }) {
  const { t } = useI18n()
  const u = t.updates

  return (
    <div className="grid gap-5 px-6 pb-6 pt-7 pr-8">
      <div className="flex flex-col items-center gap-3 text-center">
        <AlertCircle className="size-8 text-amber-500" />

        <DialogTitle className="text-center text-xl">{u.guiSkewTitle}</DialogTitle>
        <DialogDescription className="max-w-prose text-center text-sm leading-5 text-muted-foreground">
          {message || u.guiSkewBody}
        </DialogDescription>
      </div>

      <Button className="font-semibold" onClick={onDone} size="lg" variant="secondary">
        {u.done}
      </Button>
    </div>
  )
}

function ApplyingView({ apply, isBackend }: { apply: UpdateApplyState; isBackend: boolean }) {
  const { t } = useI18n()
  const u = t.updates
  const label = u.stages[apply.stage as DesktopUpdateStage] ?? u.stages.idle
  const body = isBackend ? u.applyingBodyBackend : u.applyingBody
  const currentMessage = apply.message.trim()
  const recentLog = apply.log.slice(-4)

  const percent =
    typeof apply.percent === 'number' && Number.isFinite(apply.percent)
      ? Math.max(2, Math.min(100, Math.round(apply.percent)))
      : null

  return (
    <div className="grid gap-5 px-6 pb-6 pt-7">
      <div className="flex flex-col items-center gap-3 text-center">
        <Loader className="size-16" label={label} type="lemniscate-bloom" />

        <DialogTitle className="text-center text-xl">{label}</DialogTitle>
        <DialogDescription className="text-center text-sm">{body}</DialogDescription>

        {currentMessage ? (
          <p className="max-w-lg break-words text-center text-xs leading-5 text-muted-foreground">{currentMessage}</p>
        ) : null}
      </div>

      <div className="h-2 overflow-hidden rounded-full bg-muted">
        <div
          className={cn(
            'h-full rounded-full bg-primary transition-[width] duration-300 ease-out',
            percent === null && 'w-1/3 animate-pulse'
          )}
          style={percent !== null ? { width: `${percent}%` } : undefined}
        />
      </div>

      {recentLog.length > 1 ? (
        <div className="max-h-24 overflow-hidden rounded-md border border-border/70 bg-muted/35 px-3 py-2 text-left font-mono text-[11px] leading-4 text-muted-foreground">
          {recentLog.map((entry, index) => (
            <div className="truncate" key={`${entry.at}-${index}`}>
              {entry.message}
            </div>
          ))}
        </div>
      ) : null}

      <p className="text-center text-xs text-muted-foreground">{u.applyingClose}</p>
    </div>
  )
}

function ErrorView({ message, onDismiss, onRetry }: { message: string; onDismiss: () => void; onRetry: () => void }) {
  const { t } = useI18n()
  const u = t.updates

  return (
    <ErrorState
      className="px-6 pb-6 pt-7 pr-8"
      description={
        <DialogDescription className="max-w-prose text-center text-sm leading-5 text-muted-foreground">
          {message || u.errorBody}
        </DialogDescription>
      }
      title={<DialogTitle className="text-center text-xl font-semibold tracking-tight">{u.errorTitle}</DialogTitle>}
    >
      <Button className="font-semibold" onClick={onRetry} size="lg">
        {u.tryAgain}
      </Button>
      <Button onClick={onDismiss} variant="text">
        {u.notNow}
      </Button>
    </ErrorState>
  )
}

function CenteredStatus({
  action,
  body,
  icon,
  title
}: {
  action?: React.ReactNode
  body?: string
  icon: React.ReactNode
  title: string
}) {
  return (
    <div className="grid gap-4 px-6 pb-6 pt-8 pr-8">
      <div className="flex flex-col items-center gap-3 text-center">
        {icon}

        <DialogTitle className="text-center text-lg">{title}</DialogTitle>
        {body && <DialogDescription className="text-center text-sm">{body}</DialogDescription>}
      </div>

      {action && <div className="flex justify-center">{action}</div>}
    </div>
  )
}
