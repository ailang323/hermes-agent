export interface ManagedApprovalResult {
  candidateId?: string
  candidateSha?: string
  artifactSha256?: string
  verificationReportSha256?: string
  confirmationToken?: string
  [key: string]: unknown
}

export interface ManagedApproval {
  candidateId: string
  candidateSha: string
  artifactSha256: string
  verificationReportSha256: string
  confirmationToken: string
}

const CANDIDATE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
const SHA_PATTERN = /^[0-9a-f]{40}$/
const HASH_PATTERN = /^[0-9a-f]{64}$/

export interface ManagedApprovalSummary {
  candidateId: string
  candidateSha: string
  artifactSha256: string
  verificationReportSha256: string
}

export function buildManagedUpdateConfirmationOptions(summary: ManagedApprovalSummary, locale = 'en') {
  const chinese = locale.toLowerCase().startsWith('zh')

  return {
    type: 'warning' as const,
    buttons: chinese ? ['取消', '安装已验证更新'] : ['Cancel', 'Install verified update'],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
    title: chinese ? '安装已验证的 Hermes 更新？' : 'Install verified Hermes update?',
    message: chinese
      ? '这将替换当前 Hermes Desktop 应用并重新启动。'
      : 'This will replace the active Hermes Desktop bundle and restart the app.',
    detail: chinese
      ? [
          `候选：${summary.candidateId}`,
          `提交：${summary.candidateSha}`,
          `制品 SHA-256：${summary.artifactSha256}`,
          `验证报告 SHA-256：${summary.verificationReportSha256}`
        ].join('\n')
      : [
          `Candidate: ${summary.candidateId}`,
          `Commit: ${summary.candidateSha}`,
          `Artifact SHA-256: ${summary.artifactSha256}`,
          `Verification report SHA-256: ${summary.verificationReportSha256}`
        ].join('\n')
  }
}

export class ManagedUpdateApprovalVault {
  private readonly approvals = new Map<string, ManagedApproval>()

  retain<T extends ManagedApprovalResult>(
    result: T
  ): Omit<T, 'confirmationToken' | 'verificationReportSha256'> {
    const candidateId = result.candidateId ?? ''
    const candidateSha = result.candidateSha ?? ''
    const artifactSha256 = result.artifactSha256 ?? ''
    const verificationReportSha256 = result.verificationReportSha256 ?? ''
    const confirmationToken = result.confirmationToken ?? ''

    if (
      !CANDIDATE_ID_PATTERN.test(candidateId) ||
      !SHA_PATTERN.test(candidateSha) ||
      !HASH_PATTERN.test(artifactSha256) ||
      !HASH_PATTERN.test(verificationReportSha256) ||
      !HASH_PATTERN.test(confirmationToken)
    ) {
      throw new Error('Managed update approval result is invalid.')
    }

    this.approvals.set(candidateId, {
      candidateId,
      candidateSha,
      artifactSha256,
      verificationReportSha256,
      confirmationToken
    })

    const {
      confirmationToken: _removedToken,
      verificationReportSha256: _removedReportHash,
      ...sanitized
    } = result

    return sanitized
  }

  peek(candidateId: string): ManagedApproval | null {
    return this.approvals.get(candidateId) ?? null
  }

  consume(candidateId: string): ManagedApproval | null {
    const approval = this.peek(candidateId)

    if (approval) {
      this.approvals.delete(candidateId)
    }

    return approval
  }

  async authorize(
    candidateId: string,
    confirm: (summary: ManagedApprovalSummary) => Promise<boolean>
  ): Promise<ManagedApproval | null> {
    const approval = this.peek(candidateId)

    if (!approval) {
      throw new Error('Managed update approval is not available.')
    }

    const accepted = await confirm({
      candidateId: approval.candidateId,
      candidateSha: approval.candidateSha,
      artifactSha256: approval.artifactSha256,
      verificationReportSha256: approval.verificationReportSha256
    })

    return accepted ? approval : null
  }

  revoke(candidateId: string): boolean {
    return this.approvals.delete(candidateId)
  }
}
