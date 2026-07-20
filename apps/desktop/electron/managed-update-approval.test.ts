import { describe, expect, it } from 'vitest'

import {
  buildManagedUpdateConfirmationOptions,
  ManagedUpdateApprovalVault
} from './managed-update-approval'

describe('ManagedUpdateApprovalVault', () => {
  it('builds a fail-safe native confirmation that shows exact verification evidence', () => {
    const options = buildManagedUpdateConfirmationOptions({
      candidateId: 'candidate-dialog',
      candidateSha: '7'.repeat(40),
      artifactSha256: '8'.repeat(64)
    })

    expect(options).toMatchObject({
      type: 'warning',
      buttons: ['Cancel', 'Install verified update'],
      defaultId: 0,
      cancelId: 0,
      noLink: true
    })
    expect(options.detail).toContain('7'.repeat(40))
    expect(options.detail).toContain('8'.repeat(64))
  })

  it('localizes the native confirmation for a Chinese system locale while keeping cancel as the safe default', () => {
    const options = buildManagedUpdateConfirmationOptions(
      {
        candidateId: 'candidate-dialog-zh',
        candidateSha: '9'.repeat(40),
        artifactSha256: 'a'.repeat(64)
      },
      'zh-CN'
    )

    expect(options).toMatchObject({
      buttons: ['取消', '安装已验证更新'],
      defaultId: 0,
      cancelId: 0,
      title: '安装已验证的 Hermes 更新？'
    })
    expect(options.detail).toContain('候选：candidate-dialog-zh')
    expect(options.detail).toContain('提交：')
    expect(options.detail).toContain('制品 SHA-256：')
  })

  it('keeps the approval token in main-only state and strips it from renderer data', () => {
    const vault = new ManagedUpdateApprovalVault()

    const result = {
      ok: true,
      managed: true,
      candidateId: 'desktop-1234-abcd',
      candidateSha: 'c'.repeat(40),
      artifactSha256: 'd'.repeat(64),
      confirmationToken: 'e'.repeat(64)
    }

    const sanitized = vault.retain(result)

    expect(sanitized).not.toHaveProperty('confirmationToken')
    expect(vault.peek(result.candidateId)).toEqual({
      candidateId: result.candidateId,
      candidateSha: result.candidateSha,
      artifactSha256: result.artifactSha256,
      confirmationToken: result.confirmationToken
    })
  })

  it('consumes an approval exactly once and supports explicit revocation', () => {
    const vault = new ManagedUpdateApprovalVault()

    const first = {
      candidateId: 'candidate-one',
      candidateSha: 'a'.repeat(40),
      artifactSha256: 'b'.repeat(64),
      confirmationToken: 'c'.repeat(64)
    }

    const second = {
      candidateId: 'candidate-two',
      candidateSha: 'd'.repeat(40),
      artifactSha256: 'e'.repeat(64),
      confirmationToken: 'f'.repeat(64)
    }

    vault.retain(first)
    vault.retain(second)
    expect(vault.consume(first.candidateId)).toMatchObject(first)
    expect(vault.consume(first.candidateId)).toBeNull()
    expect(vault.revoke(second.candidateId)).toBe(true)
    expect(vault.peek(second.candidateId)).toBeNull()
  })

  it('authorizes exactly once without exposing the token to the native confirmation callback', async () => {
    const vault = new ManagedUpdateApprovalVault()

    const approval = {
      candidateId: 'candidate-authorize',
      candidateSha: '1'.repeat(40),
      artifactSha256: '2'.repeat(64),
      confirmationToken: '3'.repeat(64)
    }

    vault.retain(approval)

    const authorized = await vault.authorize(approval.candidateId, async summary => {
      expect(summary).toEqual({
        candidateId: approval.candidateId,
        candidateSha: approval.candidateSha,
        artifactSha256: approval.artifactSha256
      })
      expect(summary).not.toHaveProperty('confirmationToken')

      return true
    })

    expect(authorized).toEqual(approval)
    await expect(vault.authorize(approval.candidateId, async () => true)).rejects.toThrow('not available')
  })

  it('revokes the approval when native confirmation is cancelled', async () => {
    const vault = new ManagedUpdateApprovalVault()

    const approval = {
      candidateId: 'candidate-cancel',
      candidateSha: '4'.repeat(40),
      artifactSha256: '5'.repeat(64),
      confirmationToken: '6'.repeat(64)
    }

    vault.retain(approval)

    await expect(vault.authorize(approval.candidateId, async () => false)).resolves.toBeNull()
    expect(vault.peek(approval.candidateId)).toBeNull()
  })

  it('rejects malformed candidate metadata and tokens', () => {
    const vault = new ManagedUpdateApprovalVault()

    expect(() =>
      vault.retain({
        candidateId: '../escape',
        candidateSha: 'a'.repeat(40),
        artifactSha256: 'b'.repeat(64),
        confirmationToken: 'c'.repeat(64)
      })
    ).toThrow('invalid')
  })
})
