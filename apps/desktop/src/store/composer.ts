import { atom } from 'nanostores'

import { triggerHaptic } from '@/lib/haptics'

import { $queuedPromptsBySession } from './composer-queue'

export interface ComposerAttachment {
  id: string
  kind: 'image' | 'file' | 'folder' | 'terminal' | 'url'
  label: string
  detail?: string
  refText?: string
  previewUrl?: string
  path?: string
  attachedSessionId?: string
  /** Set while the file/image bytes are being staged into the session
   * workspace (remote upload or local stage), and 'error' if that failed.
   * Drives the spinner / error state on the composer attachment card. */
  uploadState?: 'uploading' | 'error'
}

export const $composerDraft = atom('')
export const $composerAttachments = atom<ComposerAttachment[]>([])

// Draft TEXT is persisted (see SESSION_DRAFTS_STORAGE_KEY), so a draft holding a
// `@selection:_selection` chip outlives a restart. The text that chip stands for
// has to be persisted with it: otherwise the chip comes back as a dud — it still
// looks like a reference, has nothing to show on hover, and
// composerContextBlocksFromDraft() silently contributes NOTHING to the prompt.
// The user believes they attached context and never sent any.
export const CONTEXT_REFS_STORAGE_KEY = 'hermes:composer-context-refs:v1'

/** Total serialized budget. Quoted selections can be whole messages, and this
 *  shares a ~5MB localStorage quota with drafts and other stores. */
const MAX_PERSISTED_CONTEXT_REF_BYTES = 256 * 1024

function loadPersistedContextReferences(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(CONTEXT_REFS_STORAGE_KEY)
    const parsed = raw ? JSON.parse(raw) : null

    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return {}
    }

    const entries = Object.entries(parsed as Record<string, unknown>).filter(
      (entry): entry is [string, string] => typeof entry[1] === 'string' && entry[1].length > 0
    )

    return Object.fromEntries(entries)
  } catch {
    return {}
  }
}

export const $composerContextReferences = atom<Record<string, string>>(loadPersistedContextReferences())
// Back-compatible alias for existing terminal-selection callers.
export const $composerTerminalSelections = $composerContextReferences

function persistContextReferences(refs: Record<string, string>) {
  try {
    const entries = Object.entries(refs)

    if (entries.length === 0) {
      window.localStorage.removeItem(CONTEXT_REFS_STORAGE_KEY)

      return
    }

    // Drop oldest-first until the payload fits. Insertion order is quote order,
    // so the reference the user just made is the last one to be sacrificed.
    let kept = entries

    while (kept.length > 0 && JSON.stringify(Object.fromEntries(kept)).length > MAX_PERSISTED_CONTEXT_REF_BYTES) {
      kept = kept.slice(1)
    }

    if (kept.length === 0) {
      window.localStorage.removeItem(CONTEXT_REFS_STORAGE_KEY)

      return
    }

    window.localStorage.setItem(CONTEXT_REFS_STORAGE_KEY, JSON.stringify(Object.fromEntries(kept)))
  } catch {
    // Best-effort only — quota/private-mode must never break quoting.
  }
}

$composerContextReferences.listen(persistContextReferences)

// ---------------------------------------------------------------------------
// Composer scopes — one live attachment set PER MOUNTED COMPOSER. The main
// chat's scope wraps the module-level atom above (all existing readers keep
// working); each session tile creates its own so two composers on screen
// never share chips. Draft text needs no scope: it lives in each ChatBar's
// DOM + draftRef and stashes per session key already.
// ---------------------------------------------------------------------------

export interface ComposerAttachmentScope {
  $attachments: ReturnType<typeof atom<ComposerAttachment[]>>
  add(attachment: ComposerAttachment): void
  clear(): void
  remove(id: string): ComposerAttachment | null
  setUploadState(id: string, uploadState?: ComposerAttachment['uploadState']): void
  update(attachment: ComposerAttachment): boolean
}

export function createComposerAttachmentScope($attachments = atom<ComposerAttachment[]>([])): ComposerAttachmentScope {
  return {
    $attachments,
    add(attachment) {
      const previous = $attachments.get()
      const next = upsertAttachment(previous, attachment)
      $attachments.set(next)

      if (next.length > previous.length && attachment.kind !== 'url') {
        triggerHaptic('selection')
      }
    },
    clear() {
      $attachments.set([])
    },
    remove(id) {
      const current = $attachments.get()
      const removed = current.find(attachment => attachment.id === id) || null
      $attachments.set(current.filter(attachment => attachment.id !== id))

      return removed
    },
    setUploadState(id, uploadState) {
      const current = $attachments.get()
      const index = current.findIndex(attachment => attachment.id === id)

      if (index < 0) {
        return
      }

      const next = [...current]
      next[index] = { ...next[index]!, uploadState }
      $attachments.set(next)
    },
    update(attachment) {
      const current = $attachments.get()
      const index = current.findIndex(item => item.id === attachment.id)

      if (index < 0) {
        return false
      }

      const next = [...current]
      next[index] = attachment
      $attachments.set(next)

      return true
    }
  }
}

/** The main chat's scope — the module-level atom, so every existing
 *  `$composerAttachments` reader/writer IS this scope. */
export const mainComposerScope = createComposerAttachmentScope($composerAttachments)

// Per-thread draft stash for the decoupled composer. Session lifecycle never
// touches this — only ChatBar's scope swap reads/writes it. Text mirrors to
// localStorage; attachments are memory-only (blobs, upload state).
export const SESSION_DRAFTS_STORAGE_KEY = 'hermes:composer-drafts:v3'

const NEW_SESSION_DRAFT_KEY = '__new__'
const MAX_PERSISTED_DRAFTS = 50
const EMPTY_SESSION_DRAFT: SessionDraft = { attachments: [], text: '' }

export interface SessionDraft {
  attachments: ComposerAttachment[]
  text: string
}

const draftKey = (scope: string | null | undefined) => scope?.trim() || NEW_SESSION_DRAFT_KEY

const cloneDraft = (draft: SessionDraft): SessionDraft => ({
  attachments: draft.attachments.map(attachment => ({ ...attachment })),
  text: draft.text
})

function loadPersistedDraftTexts(): [string, SessionDraft][] {
  try {
    const raw = window.localStorage.getItem(SESSION_DRAFTS_STORAGE_KEY)

    if (!raw) {
      return []
    }

    return Object.entries(JSON.parse(raw) as Record<string, string>).map(([key, text]) => [
      key,
      { attachments: [], text }
    ])
  } catch {
    return []
  }
}

const draftsBySession = new Map<string, SessionDraft>(loadPersistedDraftTexts())

/**
 * Keys some composer-owned text still mentions — every persisted per-session
 * draft, persisted queued prompt, and the live composer text.
 *
 * A reference whose chip the user deleted is no longer live, which is what lets
 * its label be handed out again. Without this, `nextComposerContextReferenceLabel`
 * treated every reference ever created as taken and the label climbed forever
 * (`_selection-2`, `-3`, …) even though the composer was empty.
 *
 * Liveness is derived rather than pruned on edit on purpose: the references map is
 * global while drafts are per-session, so reconciling against one session's draft
 * would delete another session's references — and a reference is created a tick
 * before its chip reaches the draft, so an eager prune races the insert.
 */
function liveContextReferenceKeys() {
  const keys = new Set<string>()
  const texts = [...draftsBySession.values()].map(draft => draft.text)

  texts.push($composerDraft.get())

  for (const queue of Object.values($queuedPromptsBySession.get())) {
    texts.push(...queue.map(entry => entry.text))
  }

  for (const text of texts) {
    if (!text) {
      continue
    }

    for (const ref of contextReferencesFromDraft(text)) {
      keys.add(contextReferenceKey(ref.kind, ref.label))
    }
  }

  return keys
}

/** Drop persisted references no draft mentions any more — chips deleted in an
 *  earlier session. Only safe at module load, when no insert is in flight. */
function pruneOrphanedContextReferences() {
  const current = $composerContextReferences.get()
  const keys = Object.keys(current)

  if (keys.length === 0) {
    return
  }

  const live = liveContextReferenceKeys()
  const next = Object.fromEntries(keys.filter(key => live.has(key)).map(key => [key, current[key] as string]))

  if (Object.keys(next).length !== keys.length) {
    $composerContextReferences.set(next)
  }
}

function persistDraftTexts() {
  try {
    const entries = [...draftsBySession]
      .filter(([, draft]) => draft.text)
      .slice(-MAX_PERSISTED_DRAFTS)
      .map(([key, draft]) => [key, draft.text] as const)

    if (entries.length === 0) {
      window.localStorage.removeItem(SESSION_DRAFTS_STORAGE_KEY)
    } else {
      window.localStorage.setItem(SESSION_DRAFTS_STORAGE_KEY, JSON.stringify(Object.fromEntries(entries)))
    }
  } catch {
    // Best-effort only — quota/private-mode must never break typing.
  }
}

export function stashSessionDraft(scope: string | null | undefined, text: string, attachments: ComposerAttachment[]) {
  const key = draftKey(scope)

  // Delete-then-set keeps MRU order for MAX_PERSISTED_DRAFTS eviction.
  draftsBySession.delete(key)

  if (text.trim() || attachments.length > 0) {
    draftsBySession.set(key, cloneDraft({ attachments, text }))
  }

  persistDraftTexts()
}

export function takeSessionDraft(scope: string | null | undefined): SessionDraft {
  const stashed = draftsBySession.get(draftKey(scope))

  return stashed ? cloneDraft(stashed) : EMPTY_SESSION_DRAFT
}

export const clearSessionDraft = (scope: string | null | undefined) => stashSessionDraft(scope, '', [])

/**
 * Move a stashed composer draft from one session key onto another.
 *
 * Auto-compression rotates the live stored tip id (root → continuation) while
 * the user may still be typing. Drafts keyed on the obsolete tip would otherwise
 * vanish from the composer when selection follows the new tip. No-op unless both
 * keys resolve, differ, and the source has content. Does not overwrite a
 * non-empty destination draft.
 */
export function migrateSessionDraft(fromKey: string | null | undefined, toKey: string | null | undefined): boolean {
  const from = draftKey(fromKey)
  const to = draftKey(toKey)

  if (!fromKey || !toKey || from === to) {
    return false
  }

  const source = draftsBySession.get(from)

  if (!source || (!source.text.trim() && source.attachments.length === 0)) {
    return false
  }

  const dest = draftsBySession.get(to)

  if (dest && (dest.text.trim() || dest.attachments.length > 0)) {
    return false
  }

  stashSessionDraft(toKey, source.text, source.attachments)
  clearSessionDraft(fromKey)

  return true
}

export function setComposerDraft(value: string) {
  $composerDraft.set(value)
}

export function appendComposerDraft(value: string) {
  const text = value.trim()

  if (!text) {
    return
  }

  const current = $composerDraft.get()
  const separator = current && !current.endsWith('\n') ? '\n\n' : ''

  $composerDraft.set(`${current}${separator}${text}`)
}

export function appendComposerInline(value: string) {
  const text = value.trim()

  if (!text) {
    return
  }

  const current = $composerDraft.get().trimEnd()
  const separator = current ? ' ' : ''

  $composerDraft.set(`${current}${separator}${text}`)
}

export function clearComposerDraft() {
  $composerDraft.set('')
}

// Main-scope conveniences — the names the app has always used.
export const addComposerAttachment = (attachment: ComposerAttachment) => mainComposerScope.add(attachment)
export const removeComposerAttachment = (id: string) => mainComposerScope.remove(id)

/** Replace an existing attachment in place by id. No-op (returns false) when the
 * id is gone — e.g. the user removed the chip while an eager upload was still in
 * flight, so a late success must NOT resurrect it. Use this instead of
 * addComposerAttachment for async results that may land after a removal. */
export const updateComposerAttachment = (attachment: ComposerAttachment) => mainComposerScope.update(attachment)

export const clearComposerAttachments = () => mainComposerScope.clear()

/** Update only the upload state of an existing attachment (no-op if it's gone,
 * e.g. the user removed it mid-upload). Pass `undefined` to clear it. */
export const setComposerAttachmentUploadState = (id: string, uploadState?: ComposerAttachment['uploadState']) =>
  mainComposerScope.setUploadState(id, uploadState)

export type ComposerContextReferenceKind = 'selection' | 'terminal'

const CONTEXT_REF_RE = /@(terminal|selection):(`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|\S+)/g
const CONTEXT_REF_KINDS = new Set<ComposerContextReferenceKind>(['selection', 'terminal'])

function unquoteRefValue(raw: string) {
  const head = raw[0]
  const tail = raw[raw.length - 1]
  const quoted = (head === '`' && tail === '`') || (head === '"' && tail === '"') || (head === "'" && tail === "'")

  return (quoted ? raw.slice(1, -1) : raw).replace(/[,.;!?]+$/, '').trim()
}

function contextReferenceKey(kind: ComposerContextReferenceKind, label: string) {
  return `${kind}:${label}`
}

function isComposerContextReferenceKind(kind: string): kind is ComposerContextReferenceKind {
  return CONTEXT_REF_KINDS.has(kind as ComposerContextReferenceKind)
}

function contextReferencesFromDraft(draft: string) {
  const refs: Array<{ kind: ComposerContextReferenceKind; label: string }> = []
  const seen = new Set<string>()

  for (const match of draft.matchAll(CONTEXT_REF_RE)) {
    const kind = match[1] || ''
    const label = unquoteRefValue(match[2] || '')

    if (!isComposerContextReferenceKind(kind) || !label) {
      continue
    }

    const key = contextReferenceKey(kind, label)

    if (seen.has(key)) {
      continue
    }

    seen.add(key)
    refs.push({ kind, label })
  }

  return refs
}

function setComposerContextReference(kind: ComposerContextReferenceKind, label: string, text: string) {
  const nextLabel = label.trim()
  const nextText = text.trim()

  if (!nextLabel || !nextText) {
    return
  }

  const key = contextReferenceKey(kind, nextLabel)
  const current = $composerContextReferences.get()

  if (current[key] === nextText) {
    return
  }

  $composerContextReferences.set({
    ...current,
    [key]: nextText
  })
}

export function nextComposerContextReferenceLabel(kind: ComposerContextReferenceKind, baseLabel: string) {
  const base = baseLabel.trim() || (kind === 'selection' ? '_selection' : 'selection')
  // Only labels a draft still shows are taken. Reusing a stale label is correct:
  // setComposerContextReference overwrites its text, and nothing references it.
  const taken = liveContextReferenceKeys()

  // Always suffixed, starting at 1. A bare first label followed by `-2` reads as
  // a numbering glitch; a plain `@selection:_selection` chip that has no counter
  // beside it looks unrelated to the `-2` next to it. Older drafts holding the
  // un-suffixed form keep working — the label is just a key, so `_selection`
  // stays resolvable while it is still live.
  for (let index = 1; index < 1000; index += 1) {
    const candidate = `${base}-${index}`

    if (taken.has(contextReferenceKey(kind, candidate))) {
      continue
    }

    // A live un-suffixed label (written before numbering started at 1) occupies
    // slot 1. Handing out `_selection-1` beside it would show two chips that read
    // as unrelated rather than as a sequence.
    if (index === 1 && taken.has(contextReferenceKey(kind, base))) {
      continue
    }

    return candidate
  }

  return `${base}-${Date.now()}`
}

export function setComposerTerminalSelection(label: string, text: string) {
  setComposerContextReference('terminal', label, text)
}

export function setComposerSelectionReference(label: string, text: string) {
  setComposerContextReference('selection', label, text)
}

/** Max characters surfaced in a chip tooltip. A quoted selection can be the whole
 *  message; an unbounded native tooltip would cover the window. */
const CONTEXT_REF_PREVIEW_LIMIT = 600

/**
 * The referenced text behind a composer chip, for hover preview.
 *
 * A `@selection:` / `@terminal:` chip only carries a synthetic label
 * (`_selection`, `_selection-2`) — the quoted text lives here in the store, so
 * the chip has nothing readable in it. Callers use this to show what was quoted.
 * Returns '' when the label is unknown, so a caller can skip the tooltip.
 */
export function composerContextReferenceText(kind: string, label: string) {
  if (!isComposerContextReferenceKind(kind)) {
    return ''
  }

  const text = $composerContextReferences.get()[contextReferenceKey(kind, label.trim())] || ''

  return text.length > CONTEXT_REF_PREVIEW_LIMIT ? `${text.slice(0, CONTEXT_REF_PREVIEW_LIMIT)}…` : text
}

export function reconcileComposerContextReferences(draft: string) {
  const current = $composerContextReferences.get()
  const keys = liveContextReferenceKeys()

  for (const ref of contextReferencesFromDraft(draft)) {
    keys.add(contextReferenceKey(ref.kind, ref.label))
  }

  let changed = false
  const next: Record<string, string> = {}

  for (const [key, text] of Object.entries(current)) {
    if (!keys.has(key)) {
      changed = true

      continue
    }

    next[key] = text
  }

  if (changed) {
    $composerContextReferences.set(next)
  }
}

function contextBlock(kind: ComposerContextReferenceKind, label: string, text: string) {
  if (kind === 'terminal') {
    return `\`\`\`terminal\n${text}\n\`\`\``
  }

  return `Selected context (${label}):\n\`\`\`text\n${text}\n\`\`\``
}

export function composerContextBlocksFromDraft(draft: string) {
  const refs = contextReferencesFromDraft(draft)

  if (refs.length === 0) {
    return []
  }

  const selections = $composerContextReferences.get()

  return refs.flatMap(({ kind, label }) => {
    const text = selections[contextReferenceKey(kind, label)]?.trim()

    if (!text) {
      return []
    }

    return contextBlock(kind, label, text)
  })
}

export function clearComposerContextReferences() {
  if (Object.keys($composerContextReferences.get()).length === 0) {
    return
  }

  $composerContextReferences.set({})
}

export const reconcileComposerTerminalSelections = reconcileComposerContextReferences
export const terminalContextBlocksFromDraft = composerContextBlocksFromDraft
export const clearComposerTerminalSelections = clearComposerContextReferences

function upsertAttachment(attachments: ComposerAttachment[], attachment: ComposerAttachment) {
  const index = attachments.findIndex(item => item.id === attachment.id)

  if (index < 0) {
    return [...attachments, attachment]
  }

  const next = [...attachments]
  next[index] = attachment

  return next
}

// Runs LAST on purpose: this reads CONTEXT_REF_RE (via contextReferencesFromDraft),
// a `const` declared further up. Calling it where the helper is defined puts the
// call above that initializer and throws on the temporal dead zone at import.
pruneOrphanedContextReferences()
