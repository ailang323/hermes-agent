import { useQuery } from '@tanstack/react-query'
import type { FC } from 'react'

import {
  CODEX_PROVIDER,
  codexUsageQueryOptions,
  normalizeCodexUsageProfile,
  normalizeCodexUsageProvider
} from '@/lib/codex-usage-query'
import { cn } from '@/lib/utils'

interface CodexQuotaCardProps {
  enabled: boolean
  profile: string
  provider: string
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value)))
}

/** Hover card body showing profile-scoped OpenAI Codex rate-limit quota. */
export const CodexQuotaCard: FC<CodexQuotaCardProps> = ({ enabled, profile, provider }) => {
  const normalizedProfile = normalizeCodexUsageProfile(profile)
  const normalizedProvider = normalizeCodexUsageProvider(provider)
  const usage = useQuery(codexUsageQueryOptions({ enabled, profile, provider }))

  if (!enabled || normalizedProvider !== CODEX_PROVIDER) {
    return null
  }

  if (usage.isPending) {
    return (
      <div className="flex items-center gap-2 px-3 py-2 text-xs text-(--ui-text-tertiary)">
        <span className="inline-block size-3 animate-spin rounded-full border-2 border-(--ui-stroke-tertiary) border-t-(--ui-text-secondary)" />
        Loading quota…
      </div>
    )
  }

  const data = usage.data

  if (!data?.available) {
    const message =
      data?.error ?? (usage.error instanceof Error ? usage.error.message : 'Codex quota unavailable')

    return <div className="px-3 py-2 text-xs text-(--ui-text-tertiary)">{message}</div>
  }

  return (
    <div className="min-w-52 max-w-64 select-none px-3 py-2.5 text-xs">
      <div className="mb-2 font-medium text-(--ui-text-secondary)">
        OpenAI Codex{data.plan ? ` · ${data.plan}` : ''}
      </div>
      <div className="mb-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 rounded-md bg-(--ui-control-background) px-2 py-1.5 text-[0.65rem] leading-snug">
        <span className="text-(--ui-text-tertiary)">Profile</span>
        <span className="break-all text-(--ui-text-secondary)">{normalizedProfile}</span>
        <span className="text-(--ui-text-tertiary)">Account</span>
        <span className="break-all text-(--ui-text-secondary)">{data.account_email?.trim() || 'Unavailable'}</span>
      </div>
      {data.windows.length === 0 ? (
        <div className="text-(--ui-text-tertiary)">No rate-limit data</div>
      ) : (
        <div className="space-y-2">
          {data.windows.map(window => {
            const used = clampPercent(window.used_percent ?? 0)
            const remaining = clampPercent(window.remaining_percent ?? 100 - used)

            const colorClass =
              remaining <= 15 ? 'bg-red-500' : remaining <= 40 ? 'bg-amber-400' : 'bg-emerald-500'

            return (
              <div key={window.label}>
                <div className="mb-0.5 flex items-center justify-between gap-3">
                  <span className="text-(--ui-text-tertiary)">{window.label}</span>
                  <span className="tabular-nums text-(--ui-text-secondary)">{remaining}% left</span>
                </div>
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-(--ui-control-background)">
                  <div
                    className={cn('h-full rounded-full transition-all', colorClass)}
                    style={{ width: `${remaining}%` }}
                  />
                </div>
                <div className="mt-0.5 text-[0.65rem] leading-none text-(--ui-text-tertiary)">{used}% used</div>
              </div>
            )
          })}
        </div>
      )}
      {(data.details?.length ?? 0) > 0 && (
        <div className="mt-2 space-y-0.5 border-t border-(--ui-stroke-tertiary) pt-2">
          {data.details?.map((detail, index) => (
            <div className="text-[0.65rem] leading-snug text-(--ui-text-tertiary)" key={index}>
              {detail}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
