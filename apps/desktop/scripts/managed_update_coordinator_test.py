from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest.mock import patch

import scripts.managed_update_coordinator as coordinator
from scripts.managed_update_coordinator import (
    CandidateResult,
    InstallResult,
    ManagedUpdateManifest,
    ManifestError,
    VerifiedCandidate,
    _commit_identities,
    _hash_artifact,
    _swap_app_bundle,
    _write_candidate_report,
    accept_candidate_review,
    approval_token,
    cancel_candidate,
    inspect_repository,
    install_verified_candidate,
    load_candidate,
    load_manifest,
    load_verified_candidate,
    manifest_digest,
    main,
    prepare_candidate,
    recover_install_transaction,
    recover_pending_install_transactions,
    verify_candidate,
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ['git', *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class ManifestTests(unittest.TestCase):
    def test_fetch_uses_a_fully_qualified_remote_branch_refspec(self) -> None:
        manifest = ManagedUpdateManifest(
            schema=1,
            mode='managed-patch-stack',
            worktree=Path('/tmp/managed-update-worktree'),
            branch='feat/longer-stable-v2',
            upstream='upstream/main',
            installed_app=Path('/Applications/Hermes.app'),
            features=(),
            verification_commands=(),
            artifact=None,
        )

        calls: list[tuple[object, ...]] = []
        original = coordinator._run_git
        coordinator._run_git = lambda *args: calls.append(args) or ''
        try:
            coordinator._fetch_configured_upstream(manifest)
        finally:
            coordinator._run_git = original

        self.assertEqual(
            calls,
            [
                (
                    manifest.worktree,
                    'fetch',
                    '--prune',
                    'upstream',
                    'refs/heads/main:refs/remotes/upstream/main',
                )
            ],
        )

    def test_load_manifest_returns_normalized_managed_patch_stack_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            worktree = root / 'worktree'
            app = root / 'Hermes.app'
            worktree.mkdir()
            app.mkdir()
            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(worktree),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(app),
                        'features': [
                            {
                                'id': 'plugin-locale',
                                'commit_subject': 'feat(desktop): expose the active locale to plugins',
                            }
                        ],
                    }
                ),
                encoding='utf-8',
            )

            manifest = load_manifest(manifest_path)

            self.assertEqual(manifest.schema, 1)
            self.assertEqual(manifest.mode, 'managed-patch-stack')
            self.assertEqual(manifest.worktree, worktree.resolve())
            self.assertEqual(manifest.installed_app, app.resolve())
            self.assertEqual(manifest.branch, 'feat/longer-stable-v2')
            self.assertEqual(manifest.upstream, 'upstream/main')
            self.assertEqual(manifest.features[0].id, 'plugin-locale')

    def test_load_manifest_rejects_option_like_git_refs(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            worktree = root / 'worktree'
            app = root / 'Hermes.app'
            worktree.mkdir()
            app.mkdir()
            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(worktree),
                        'branch': '--help',
                        'upstream': 'upstream/main',
                        'installed_app': str(app),
                    }
                ),
                encoding='utf-8',
            )

            with self.assertRaisesRegex(ManifestError, 'branch'):
                load_manifest(manifest_path)


class RepositoryInspectionTests(unittest.TestCase):
    def test_inspect_repository_reports_patch_stack_and_dirty_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            worktree = root / 'worktree'
            app = root / 'Hermes.app'
            worktree.mkdir()
            app.mkdir()
            git(worktree, 'init', '-b', 'feat/longer-stable-v2')
            git(worktree, 'config', 'user.name', 'Managed Update Test')
            git(worktree, 'config', 'user.email', 'managed-update@example.invalid')
            (worktree / 'base.txt').write_text('base\n', encoding='utf-8')
            git(worktree, 'add', 'base.txt')
            git(worktree, 'commit', '-m', 'base')
            base_sha = git(worktree, 'rev-parse', 'HEAD')
            git(worktree, 'update-ref', 'refs/remotes/upstream/main', base_sha)
            (worktree / 'custom.txt').write_text('custom\n', encoding='utf-8')
            git(worktree, 'add', 'custom.txt')
            git(worktree, 'commit', '-m', 'feat: custom behavior')

            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(worktree),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(app),
                        'features': [],
                    }
                ),
                encoding='utf-8',
            )
            manifest = load_manifest(manifest_path)

            clean = inspect_repository(manifest)

            self.assertEqual(clean.current_branch, 'feat/longer-stable-v2')
            self.assertEqual(clean.original_sha, git(worktree, 'rev-parse', 'HEAD'))
            self.assertEqual(clean.upstream_sha, base_sha)
            self.assertEqual(clean.ahead, 1)
            self.assertEqual(clean.behind, 0)
            self.assertTrue(clean.clean)
            self.assertEqual(clean.custom_commit_subjects, ('feat: custom behavior',))

            (worktree / 'untracked.txt').write_text('dirty\n', encoding='utf-8')

            dirty = inspect_repository(manifest)
            self.assertFalse(dirty.clean)


class CandidatePreparationTests(unittest.TestCase):
    def test_prepare_candidate_rebases_only_an_isolated_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            worktree = root / 'active'
            app = root / 'Hermes.app'
            state_root = root / 'state'
            worktree.mkdir()
            app.mkdir()
            git(worktree, 'init', '-b', 'feat/longer-stable-v2')
            git(worktree, 'config', 'user.name', 'Managed Update Test')
            git(worktree, 'config', 'user.email', 'managed-update@example.invalid')
            (worktree / 'base.txt').write_text('base\n', encoding='utf-8')
            git(worktree, 'add', 'base.txt')
            git(worktree, 'commit', '-m', 'base')
            base_sha = git(worktree, 'rev-parse', 'HEAD')
            (worktree / 'custom.txt').write_text('custom\n', encoding='utf-8')
            git(worktree, 'add', 'custom.txt')
            git(worktree, 'commit', '-m', 'feat: custom behavior')
            original_sha = git(worktree, 'rev-parse', 'HEAD')

            git(worktree, 'switch', '--detach', base_sha)
            (worktree / 'upstream.txt').write_text('upstream\n', encoding='utf-8')
            git(worktree, 'add', 'upstream.txt')
            git(worktree, 'commit', '-m', 'feat: upstream behavior')
            upstream_sha = git(worktree, 'rev-parse', 'HEAD')
            git(worktree, 'update-ref', 'refs/remotes/upstream/main', upstream_sha)
            git(worktree, 'switch', 'feat/longer-stable-v2')

            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(worktree),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(app),
                        'features': [
                            {'id': 'custom', 'commit_subject': 'feat: custom behavior'}
                        ],
                    }
                ),
                encoding='utf-8',
            )
            manifest = load_manifest(manifest_path)

            result = prepare_candidate(manifest, state_root, candidate_id='candidate-1')

            self.assertEqual(result.status, 'ready')
            self.assertEqual(result.original_sha, original_sha)
            self.assertEqual(result.upstream_sha, upstream_sha)
            self.assertNotEqual(result.candidate_sha, original_sha)
            self.assertEqual(result.conflicts, ())
            self.assertEqual(git(worktree, 'rev-parse', 'HEAD'), original_sha)
            self.assertEqual(git(worktree, 'status', '--porcelain=v1'), '')
            self.assertEqual(
                git(result.worktree, 'log', '--format=%s', '--reverse', 'upstream/main..HEAD'),
                'feat: custom behavior',
            )
            report = json.loads((result.state_dir / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['candidate_sha'], result.candidate_sha)
            self.assertEqual(report['status'], 'ready')

    def test_prepare_candidate_reports_conflicts_without_touching_active_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            worktree = root / 'active'
            app = root / 'Hermes.app'
            state_root = root / 'state'
            worktree.mkdir()
            app.mkdir()
            git(worktree, 'init', '-b', 'feat/longer-stable-v2')
            git(worktree, 'config', 'user.name', 'Managed Update Test')
            git(worktree, 'config', 'user.email', 'managed-update@example.invalid')
            shared = worktree / 'shared.txt'
            shared.write_text('base\n', encoding='utf-8')
            git(worktree, 'add', 'shared.txt')
            git(worktree, 'commit', '-m', 'base')
            base_sha = git(worktree, 'rev-parse', 'HEAD')
            shared.write_text('custom\n', encoding='utf-8')
            git(worktree, 'commit', '-am', 'feat: custom shared behavior')
            original_sha = git(worktree, 'rev-parse', 'HEAD')

            git(worktree, 'switch', '--detach', base_sha)
            shared.write_text('upstream\n', encoding='utf-8')
            git(worktree, 'commit', '-am', 'feat: upstream shared behavior')
            upstream_sha = git(worktree, 'rev-parse', 'HEAD')
            git(worktree, 'update-ref', 'refs/remotes/upstream/main', upstream_sha)
            git(worktree, 'switch', 'feat/longer-stable-v2')

            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(worktree),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(app),
                        'features': [
                            {
                                'id': 'custom-shared',
                                'commit_subject': 'feat: custom shared behavior',
                            }
                        ],
                    }
                ),
                encoding='utf-8',
            )

            result = prepare_candidate(
                load_manifest(manifest_path),
                state_root,
                candidate_id='candidate-conflict',
            )

            self.assertEqual(result.status, 'conflict')
            self.assertEqual(result.conflicts, ('shared.txt',))
            self.assertEqual(git(worktree, 'rev-parse', 'HEAD'), original_sha)
            self.assertEqual(git(worktree, 'status', '--porcelain=v1'), '')
            report = json.loads((result.state_dir / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'conflict')
            self.assertEqual(report['conflicts'], ['shared.txt'])

    def test_upstream_equivalence_rejects_whitespace_only_control_flow_changes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            worktree = Path(raw_temp) / 'repository'
            worktree.mkdir()
            git(worktree, 'init', '-b', 'feature')
            git(worktree, 'config', 'user.name', 'Managed Update Test')
            git(worktree, 'config', 'user.email', 'managed-update@example.invalid')
            source = worktree / 'policy.py'
            source.write_text('if enabled:\n    permit()\n', encoding='utf-8')
            git(worktree, 'add', 'policy.py')
            git(worktree, 'commit', '-m', 'base')
            base_sha = git(worktree, 'rev-parse', 'HEAD')

            source.write_text('if enabled:\n    permit()\ndeny()\n', encoding='utf-8')
            git(worktree, 'commit', '-am', 'feat: local policy')
            local_sha = git(worktree, 'rev-parse', 'HEAD')

            git(worktree, 'switch', '--detach', base_sha)
            source.write_text('if enabled:\n    permit()\n    deny()\n', encoding='utf-8')
            git(worktree, 'commit', '-am', 'feat: upstream policy')
            upstream_sha = git(worktree, 'rev-parse', 'HEAD')
            git(worktree, 'switch', 'feature')

            self.assertEqual(
                coordinator._commit_patch_id(worktree, local_sha),
                coordinator._commit_patch_id(worktree, upstream_sha),
                'the fixture must exercise git patch-id whitespace insensitivity',
            )
            self.assertNotIn(
                local_sha,
                coordinator._upstream_equivalent_commit_shas(
                    worktree,
                    upstream_sha,
                    local_sha,
                ),
            )

    def test_resume_conflict_records_an_immutable_resolution_and_reloads_ready_candidate(self) -> None:
        resume = getattr(coordinator, 'resume_candidate_conflict', None)
        if resume is None:
            self.fail('resume_candidate_conflict API is missing')

        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            worktree = root / 'active'
            app = root / 'Hermes.app'
            state_root = root / 'state'
            worktree.mkdir()
            app.mkdir()
            git(worktree, 'init', '-b', 'feat/longer-stable-v2')
            git(worktree, 'config', 'user.name', 'Managed Update Test')
            git(worktree, 'config', 'user.email', 'managed-update@example.invalid')
            shared = worktree / 'shared.txt'
            shared.write_text('base\n', encoding='utf-8')
            git(worktree, 'add', 'shared.txt')
            git(worktree, 'commit', '-m', 'base')
            base_sha = git(worktree, 'rev-parse', 'HEAD')
            shared.write_text('custom\n', encoding='utf-8')
            git(worktree, 'commit', '-am', 'feat: custom shared behavior')
            original_sha = git(worktree, 'rev-parse', 'HEAD')

            git(worktree, 'switch', '--detach', base_sha)
            shared.write_text('upstream\n', encoding='utf-8')
            git(worktree, 'commit', '-am', 'feat: upstream shared behavior')
            upstream_sha = git(worktree, 'rev-parse', 'HEAD')
            git(worktree, 'update-ref', 'refs/remotes/upstream/main', upstream_sha)
            git(worktree, 'switch', 'feat/longer-stable-v2')

            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(worktree),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(app),
                        'artifact': 'artifact.bin',
                        'verification_commands': [],
                        'features': [
                            {
                                'id': 'custom-shared',
                                'commit_subject': 'feat: custom shared behavior',
                            }
                        ],
                    }
                ),
                encoding='utf-8',
            )
            manifest = load_manifest(manifest_path)
            candidate = prepare_candidate(manifest, state_root, candidate_id='candidate-resume')
            self.assertEqual(candidate.status, 'conflict')

            resolved_shared = candidate.worktree / 'shared.txt'
            resolved_shared.write_text('upstream\ncustom\n', encoding='utf-8')
            git(candidate.worktree, 'add', 'shared.txt')
            git(candidate.worktree, '-c', 'core.editor=true', 'rebase', '--continue')
            (candidate.worktree / 'artifact.bin').write_bytes(b'resolved artifact')

            # Simulate a process interruption after the durable conflict resolution was
            # written but before verification.json and candidate-ready were emitted.
            resolved_candidate = resume(manifest, candidate)
            self.assertEqual(resolved_candidate.status, 'ready')
            self.assertFalse((candidate.state_dir / 'verification.json').exists())

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = coordinator.main(
                    [
                        '--manifest',
                        str(manifest_path),
                        'resume-conflict',
                        '--state-root',
                        str(state_root),
                        '--candidate-id',
                        candidate.candidate_id,
                    ]
                )
            self.assertEqual(exit_code, 0)
            event = json.loads(output.getvalue())
            self.assertEqual(event['event'], 'candidate-ready')
            ready = load_candidate(candidate.state_dir)

            self.assertEqual(ready.status, 'ready')
            self.assertEqual(ready.conflicts, ())
            self.assertEqual(ready.original_sha, original_sha)
            self.assertEqual(ready.upstream_sha, upstream_sha)
            self.assertEqual(ready.original_commit_subjects, ready.candidate_commit_subjects)
            resolution_path = candidate.state_dir / 'conflict-resolution.json'
            resolution = json.loads(resolution_path.read_text(encoding='utf-8'))
            self.assertEqual(resolution['status'], 'resolved')
            self.assertEqual(resolution['conflicts'], ['shared.txt'])
            self.assertEqual(len(resolution['transformed_commits']), 1)
            self.assertEqual(resolution['transformed_commits'][0]['subject'], 'feat: custom shared behavior')
            decision_digest = coordinator._review_decision_digest(ready)
            self.assertIsInstance(decision_digest, str)
            self.assertRegex(decision_digest or '', r'^[0-9a-f]{64}$')
            self.assertEqual(load_candidate(candidate.state_dir), ready)
            self.assertEqual(git(worktree, 'rev-parse', 'HEAD'), original_sha)
            self.assertEqual(git(worktree, 'status', '--porcelain=v1'), '')

    def test_prepare_candidate_requires_review_when_upstream_absorbs_a_feature(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            worktree = root / 'active'
            app = root / 'Hermes.app'
            worktree.mkdir()
            app.mkdir()
            git(worktree, 'init', '-b', 'feat/longer-stable-v2')
            git(worktree, 'config', 'user.name', 'Managed Update Test')
            git(worktree, 'config', 'user.email', 'managed-update@example.invalid')
            (worktree / 'base.txt').write_text('base\n', encoding='utf-8')
            git(worktree, 'add', 'base.txt')
            git(worktree, 'commit', '-m', 'base')
            base_sha = git(worktree, 'rev-parse', 'HEAD')
            (worktree / 'feature.txt').write_text('same implementation\n', encoding='utf-8')
            git(worktree, 'add', 'feature.txt')
            git(worktree, 'commit', '-m', 'feat: local feature')

            git(worktree, 'switch', '--detach', base_sha)
            (worktree / 'feature.txt').write_text('same implementation\n', encoding='utf-8')
            git(worktree, 'add', 'feature.txt')
            git(worktree, 'commit', '-m', 'feat: official equivalent')
            git(
                worktree,
                'update-ref',
                'refs/remotes/upstream/main',
                git(worktree, 'rev-parse', 'HEAD'),
            )
            git(worktree, 'switch', 'feat/longer-stable-v2')

            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(worktree),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(app),
                        'features': [
                            {'id': 'local-feature', 'commit_subject': 'feat: local feature'}
                        ],
                        'artifact': 'artifact.bin',
                    }
                ),
                encoding='utf-8',
            )

            manifest = load_manifest(manifest_path)
            result = prepare_candidate(
                manifest,
                root / 'state',
                candidate_id='candidate-review',
            )

            self.assertEqual(result.status, 'review')
            self.assertEqual(len(result.recommendations), 1)
            recommendation = result.recommendations[0]
            self.assertEqual(recommendation.feature_id, 'local-feature')
            self.assertEqual(recommendation.kind, 'upstream-equivalent')
            report = json.loads((result.state_dir / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['recommendations'][0]['feature_id'], 'local-feature')

            (result.worktree / 'artifact.bin').write_bytes(b'accepted-review')
            verified = accept_candidate_review(manifest, result)
            self.assertEqual(verified.status, 'verified')
            decision = json.loads((result.state_dir / 'decision.json').read_text(encoding='utf-8'))
            self.assertEqual(decision['decision'], 'accept-upstream-equivalents')
            self.assertEqual(decision['features'], ['local-feature'])

    def test_patch_identity_preserves_duplicate_subject_multiplicity(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            worktree = root / 'active'
            app = root / 'Hermes.app'
            worktree.mkdir()
            app.mkdir()
            git(worktree, 'init', '-b', 'feat/longer-stable-v2')
            git(worktree, 'config', 'user.name', 'Managed Update Test')
            git(worktree, 'config', 'user.email', 'managed-update@example.invalid')
            (worktree / 'base.txt').write_text('base\n', encoding='utf-8')
            git(worktree, 'add', 'base.txt')
            git(worktree, 'commit', '-m', 'base')
            base_sha = git(worktree, 'rev-parse', 'HEAD')

            (worktree / 'absorbed.txt').write_text('official implementation\n', encoding='utf-8')
            git(worktree, 'add', 'absorbed.txt')
            git(worktree, 'commit', '-m', 'feat: duplicate subject')
            absorbed_sha = git(worktree, 'rev-parse', 'HEAD')
            (worktree / 'retained.txt').write_text('local behavior\n', encoding='utf-8')
            git(worktree, 'add', 'retained.txt')
            git(worktree, 'commit', '-m', 'feat: duplicate subject')
            retained_sha = git(worktree, 'rev-parse', 'HEAD')

            git(worktree, 'switch', '--detach', base_sha)
            (worktree / 'absorbed.txt').write_text('official implementation\n', encoding='utf-8')
            git(worktree, 'add', 'absorbed.txt')
            git(worktree, 'commit', '-m', 'feat: official equivalent')
            upstream_sha = git(worktree, 'rev-parse', 'HEAD')
            git(worktree, 'update-ref', 'refs/remotes/upstream/main', upstream_sha)
            git(worktree, 'switch', 'feat/longer-stable-v2')

            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(worktree),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(app),
                        'features': [],
                    }
                ),
                encoding='utf-8',
            )

            result = prepare_candidate(
                load_manifest(manifest_path),
                root / 'state',
                candidate_id='candidate-duplicate-subject',
            )

            self.assertEqual(result.status, 'review')
            self.assertEqual(len(result.recommendations), 1)
            self.assertEqual(result.recommendations[0].feature_id, f'commit-{absorbed_sha[:12]}')
            report_path = result.state_dir / 'report.json'
            report = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertEqual([item['sha'] for item in report['original_commits']], [absorbed_sha, retained_sha])
            self.assertEqual(len({item['patch_id'] for item in report['original_commits']}), 2)
            self.assertEqual(len(report['candidate_commits']), 1)
            self.assertEqual(report['candidate_commits'][0]['subject'], 'feat: duplicate subject')

            report['candidate_commits'][0]['patch_id'] = 'f' * 40
            report_path.chmod(0o600)
            report_path.write_text(json.dumps(report), encoding='utf-8')
            with self.assertRaisesRegex(ManifestError, 'patch identities'):
                load_candidate(result.state_dir)


class CandidateCancellationTests(unittest.TestCase):
    def test_cancel_removes_linked_worktree_preserves_audit_and_invalidates_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            active = root / 'active'
            state_dir = root / 'state' / 'candidate-cancel'
            worktree = state_dir / 'worktree'
            installed_app = root / 'Hermes.app'
            active.mkdir()
            state_dir.mkdir(parents=True)
            installed_app.mkdir()
            git(active, 'init', '-b', 'feat/longer-stable-v2')
            git(active, 'config', 'user.name', 'Managed Update Test')
            git(active, 'config', 'user.email', 'managed-update@example.invalid')
            (active / 'source.txt').write_text('candidate source\n', encoding='utf-8')
            git(active, 'add', 'source.txt')
            git(active, 'commit', '-m', 'candidate source')
            candidate_sha = git(active, 'rev-parse', 'HEAD')
            git(active, 'worktree', 'add', '--detach', str(worktree), candidate_sha)
            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(active),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(installed_app),
                        'features': [],
                    }
                ),
                encoding='utf-8',
            )
            manifest = load_manifest(manifest_path)
            candidate = CandidateResult(
                candidate_id='candidate-cancel',
                status='ready',
                original_sha=candidate_sha,
                upstream_sha=candidate_sha,
                candidate_sha=candidate_sha,
                state_dir=state_dir,
                worktree=worktree,
                conflicts=(),
                recommendations=(),
                original_commit_subjects=(),
                candidate_commit_subjects=(),
            )
            _write_candidate_report(candidate)

            cancellation = cancel_candidate(manifest, candidate)
            first_report = cancellation.read_bytes()

            self.assertFalse(worktree.exists())
            self.assertTrue((state_dir / 'report.json').is_file())
            self.assertEqual(json.loads(first_report)['status'], 'cancelled')
            self.assertEqual(json.loads(first_report)['candidate_id'], 'candidate-cancel')
            self.assertEqual(cancel_candidate(manifest, candidate).read_bytes(), first_report)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        '--manifest',
                        str(manifest_path),
                        'cancel',
                        '--state-root',
                        str(root / 'state'),
                        '--candidate-id',
                        'candidate-cancel',
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(stdout.getvalue())['event'], 'candidate-cancelled')
            with self.assertRaisesRegex(ManifestError, 'cancelled'):
                load_candidate(state_dir)

    def test_cancel_durably_invalidates_before_loading_a_malformed_report(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            active = root / 'active'
            installed_app = root / 'Hermes.app'
            state_root = root / 'state'
            state_dir = state_root / 'malformed-candidate'
            active.mkdir()
            installed_app.mkdir()
            state_dir.mkdir(parents=True)
            (state_dir / 'report.json').write_text('{ malformed', encoding='utf-8')
            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(active),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(installed_app),
                        'features': [],
                    }
                ),
                encoding='utf-8',
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        '--manifest',
                        str(manifest_path),
                        'cancel',
                        '--state-root',
                        str(state_root),
                        '--candidate-id',
                        'malformed-candidate',
                    ]
                )

            self.assertEqual(exit_code, 1)
            marker = json.loads((state_dir / 'cancellation-intent.json').read_text(encoding='utf-8'))
            self.assertEqual(marker['status'], 'cancelling')
            with self.assertRaisesRegex(ManifestError, 'cancelled'):
                load_candidate(state_dir)


class ArtifactHashTests(unittest.TestCase):
    def test_tree_hash_uses_unambiguous_record_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            first = root / 'first'
            second = root / 'second'
            first.mkdir()
            second.mkdir()
            (first / 'a').write_bytes(b'x')
            (first / 'bc').write_bytes(b'payload')
            (second / 'a').write_bytes(b'xb')
            (second / 'c').write_bytes(b'payload')

            self.assertNotEqual(_hash_artifact(first), _hash_artifact(second))

    def test_tree_hash_rejects_symlinks_resolving_outside_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            artifact = root / 'Hermes.app'
            artifact.mkdir()
            external = root / 'external.txt'
            external.write_text('mutable outside bytes', encoding='utf-8')
            (artifact / 'escape').symlink_to(external)

            with self.assertRaisesRegex(ManifestError, 'symlink.*escapes'):
                _hash_artifact(artifact)


class CandidateVerificationTests(unittest.TestCase):
    def test_verify_candidate_hashes_the_exact_artifact_created_by_argv_commands(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            state_dir = root / 'candidate-verify'
            worktree = state_dir / 'worktree'
            active = root / 'active'
            installed_app = root / 'Hermes.app'
            state_dir.mkdir()
            active.mkdir()
            installed_app.mkdir()
            git(active, 'init', '-b', 'feat/longer-stable-v2')
            git(active, 'config', 'user.name', 'Managed Update Test')
            git(active, 'config', 'user.email', 'managed-update@example.invalid')
            (active / 'base.txt').write_text('base\n', encoding='utf-8')
            git(active, 'add', 'base.txt')
            git(active, 'commit', '-m', 'base')
            upstream_sha = git(active, 'rev-parse', 'HEAD')
            git(active, 'update-ref', 'refs/remotes/upstream/main', upstream_sha)
            (active / 'source.txt').write_text('candidate source\n', encoding='utf-8')
            git(active, 'add', 'source.txt')
            git(active, 'commit', '-m', 'candidate source')
            candidate_sha = git(active, 'rev-parse', 'HEAD')
            git(active, 'worktree', 'add', '--detach', str(worktree), candidate_sha)
            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(root / 'active'),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(installed_app),
                        'features': [],
                        'verification_commands': [
                            {
                                'name': 'build-artifact',
                                'argv': [
                                    sys.executable,
                                    '-c',
                                    (
                                        "import os; from pathlib import Path; "
                                        f"assert os.environ.get('GITHUB_SHA') == {candidate_sha!r}; "
                                        "assert os.environ.get('GITHUB_REF_NAME') == "
                                        "'feat/longer-stable-v2'; "
                                        "Path('artifact.bin').write_bytes(b'candidate')"
                                    ),
                                ],
                                'cwd': '.',
                            }
                        ],
                        'artifact': 'artifact.bin',
                    }
                ),
                encoding='utf-8',
            )
            candidate = CandidateResult(
                candidate_id='candidate-verify',
                state_dir=state_dir,
                worktree=worktree,
                status='ready',
                original_sha=candidate_sha,
                upstream_sha=upstream_sha,
                candidate_sha=candidate_sha,
                conflicts=(),
                original_commit_subjects=('candidate source',),
                candidate_commit_subjects=('candidate source',),
                recommendations=(),
                original_commits=_commit_identities(active, upstream_sha, candidate_sha),
                candidate_commits=_commit_identities(worktree, upstream_sha, candidate_sha),
            )
            _write_candidate_report(candidate)
            immutable_report = (state_dir / 'report.json').read_bytes()
            with self.assertRaisesRegex(ManifestError, 'already exists'):
                _write_candidate_report(candidate)
            self.assertEqual((state_dir / 'report.json').read_bytes(), immutable_report)

            (worktree / 'source.txt').write_text('drifted source\n', encoding='utf-8')
            git(worktree, 'add', 'source.txt')
            git(worktree, 'commit', '-m', 'unexpected drift')
            with self.assertRaisesRegex(ManifestError, 'HEAD'):
                verify_candidate(load_manifest(manifest_path), candidate)
            git(worktree, 'reset', '--hard', candidate_sha)

            with (
                patch(
                    'scripts.managed_update_coordinator._review_decision_digest',
                    side_effect=['before-build', 'after-build'],
                ),
                self.assertRaisesRegex(ManifestError, 'changed during candidate verification'),
            ):
                verify_candidate(load_manifest(manifest_path), candidate)

            verified = verify_candidate(load_manifest(manifest_path), candidate)

            artifact = worktree / 'artifact.bin'
            self.assertEqual(verified.status, 'verified')
            self.assertEqual(verified.artifact, artifact.resolve())
            self.assertEqual(
                verified.artifact_sha256,
                hashlib.sha256(b'candidate').hexdigest(),
            )
            report = json.loads((state_dir / 'verification.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'verified')
            self.assertEqual(report['artifact_sha256'], verified.artifact_sha256)
            self.assertEqual(report['commands'][0]['name'], 'build-artifact')
            self.assertEqual(report['commands'][0]['returncode'], 0)
            restored_candidate = load_candidate(state_dir)
            restored_verified = load_verified_candidate(state_dir, restored_candidate)
            self.assertEqual(restored_candidate, candidate)
            self.assertEqual(restored_verified, verified)

            verification_path = state_dir / 'verification.json'
            verification_report = json.loads(verification_path.read_text(encoding='utf-8'))
            verification_report['commands'][0]['returncode'] = 1
            verification_path.chmod(0o600)
            verification_path.write_text(json.dumps(verification_report), encoding='utf-8')
            with self.assertRaisesRegex(ManifestError, 'failed command'):
                load_verified_candidate(state_dir, candidate)

            candidate_path = state_dir / 'report.json'
            candidate_report = json.loads(candidate_path.read_text(encoding='utf-8'))
            candidate_report['candidate_sha'] = '--help'
            candidate_path.chmod(0o600)
            candidate_path.write_text(json.dumps(candidate_report), encoding='utf-8')
            with self.assertRaisesRegex(ManifestError, 'candidate_sha'):
                load_candidate(state_dir)


class CandidateInstallationTests(unittest.TestCase):
    def test_atomic_app_swap_restores_old_app_when_backup_move_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            target = root / 'Hermes.app'
            staged = root / '.Hermes.install.app'
            backup = root / '.Hermes.rollback.app'
            target.mkdir()
            staged.mkdir()
            (target / 'marker.txt').write_text('old\n', encoding='utf-8')
            (staged / 'marker.txt').write_text('new\n', encoding='utf-8')

            def fail_backup_move(_source: Path, _destination: Path) -> None:
                raise OSError('injected backup move failure')

            with self.assertRaisesRegex(OSError, 'injected backup move failure'):
                _swap_app_bundle(
                    target,
                    staged,
                    backup,
                    lambda _target: True,
                    move_to_backup=fail_backup_move,
                )

            self.assertEqual((target / 'marker.txt').read_text(encoding='utf-8'), 'old\n')
            self.assertFalse(staged.exists())
            self.assertFalse(backup.exists())

    def test_atomic_app_swap_preserves_old_bundle_when_inner_rollback_cannot_identify_one_side(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            target = root / 'Hermes.app'
            staged = root / '.Hermes.install.app'
            backup = root / '.Hermes.rollback.app'
            target.mkdir()
            staged.mkdir()
            backup.mkdir()
            (target / 'marker.txt').write_text('old\n', encoding='utf-8')
            (staged / 'marker.txt').write_text('new\n', encoding='utf-8')
            (backup / 'marker.txt').write_text('raced\n', encoding='utf-8')

            def fail_backup_move(_source: Path, _destination: Path) -> None:
                raise OSError('injected raced backup failure')

            with self.assertRaisesRegex(ManifestError, 'atomic rollback also failed'):
                _swap_app_bundle(
                    target,
                    staged,
                    backup,
                    lambda _target: True,
                    move_to_backup=fail_backup_move,
                )

            self.assertEqual((target / 'marker.txt').read_text(encoding='utf-8'), 'new\n')
            self.assertEqual((staged / 'marker.txt').read_text(encoding='utf-8'), 'old\n')
            self.assertEqual((backup / 'marker.txt').read_text(encoding='utf-8'), 'raced\n')

    def test_install_verified_candidate_promotes_exact_commit_and_atomically_swaps_app(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            active = root / 'active'
            state_dir = root / 'state' / 'candidate-install'
            candidate_worktree = state_dir / 'worktree'
            installed_app = root / 'Applications' / 'Hermes.app'
            active.mkdir()
            state_dir.mkdir(parents=True)
            installed_app.mkdir(parents=True)
            (installed_app / 'marker.txt').write_text('old\n', encoding='utf-8')

            git(active, 'init', '-b', 'feat/longer-stable-v2')
            git(active, 'config', 'user.name', 'Managed Update Test')
            git(active, 'config', 'user.email', 'managed-update@example.invalid')
            (active / 'base.txt').write_text('base\n', encoding='utf-8')
            git(active, 'add', 'base.txt')
            git(active, 'commit', '-m', 'base')
            base_sha = git(active, 'rev-parse', 'HEAD')
            git(active, 'update-ref', 'refs/remotes/upstream/main', base_sha)
            (active / 'custom.txt').write_text('old custom\n', encoding='utf-8')
            git(active, 'add', 'custom.txt')
            git(active, 'commit', '-m', 'feat: custom behavior')
            original_sha = git(active, 'rev-parse', 'HEAD')
            (active / 'candidate.txt').write_text('candidate\n', encoding='utf-8')
            git(active, 'add', 'candidate.txt')
            git(active, 'commit', '-m', 'feat: rebased candidate')
            candidate_sha = git(active, 'rev-parse', 'HEAD')
            git(active, 'reset', '--hard', original_sha)
            git(active, 'worktree', 'add', '--detach', str(candidate_worktree), candidate_sha)

            artifact = candidate_worktree / 'artifact' / 'Hermes.app'
            artifact.mkdir(parents=True)
            (artifact / 'marker.txt').write_text('new\n', encoding='utf-8')
            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(active),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(installed_app),
                        'features': [],
                        'verification_commands': [],
                        'artifact': 'artifact/Hermes.app',
                    }
                ),
                encoding='utf-8',
            )
            manifest = load_manifest(manifest_path)
            candidate = CandidateResult(
                candidate_id='candidate-install',
                state_dir=state_dir,
                worktree=candidate_worktree,
                status='ready',
                original_sha=original_sha,
                upstream_sha=base_sha,
                candidate_sha=candidate_sha,
                conflicts=(),
                original_commit_subjects=('feat: custom behavior',),
                candidate_commit_subjects=('feat: custom behavior', 'feat: rebased candidate'),
                recommendations=(),
                original_commits=_commit_identities(active, base_sha, original_sha),
                candidate_commits=_commit_identities(candidate_worktree, base_sha, candidate_sha),
            )
            verified = verify_candidate(manifest, candidate)

            def copy_test_bundle(source: Path, target: Path) -> None:
                shutil.copytree(source, target, symlinks=True)

            def install_current_candidate() -> InstallResult:
                return install_verified_candidate(
                    manifest,
                    candidate,
                    verified,
                    confirmation_token=approval_token(manifest, candidate, verified),
                    confirmed_candidate_sha=candidate.candidate_sha,
                    confirmed_artifact_sha256=verified.artifact_sha256,
                    copy_bundle=copy_test_bundle,
                    health_check=lambda target: (target / 'marker.txt').read_text(
                        encoding='utf-8'
                    )
                    == 'new\n',
                )

            other_app = root / 'Applications' / 'Other.app'
            other_app.mkdir()
            (other_app / 'version.txt').write_text('unrelated\n', encoding='utf-8')
            retargeted_manifest = replace(manifest, installed_app=other_app)
            with self.assertRaisesRegex(ManifestError, 'manifest changed'):
                install_verified_candidate(
                    retargeted_manifest,
                    candidate,
                    verified,
                    confirmation_token=approval_token(manifest, candidate, verified),
                    confirmed_candidate_sha=candidate.candidate_sha,
                    confirmed_artifact_sha256=verified.artifact_sha256,
                    copy_bundle=copy_test_bundle,
                    health_check=lambda _target: True,
                )
            self.assertEqual(git(active, 'rev-parse', 'HEAD'), original_sha)
            self.assertEqual((other_app / 'version.txt').read_text(encoding='utf-8'), 'unrelated\n')

            forged_recovery = state_dir / 'recovery.json'
            forged_recovery.write_text('{"schema":1,"status":"rolled-back"}', encoding='utf-8')
            with self.assertRaisesRegex(ManifestError, 'pre-existing install or recovery report'):
                install_current_candidate()
            forged_recovery.unlink()

            prior_state = state_dir.parent / 'candidate-prior'
            prior_state.mkdir()
            prior_transaction_path = prior_state / 'transaction.json'
            prior_transaction_path.write_text(
                json.dumps({'started_at': 123}), encoding='utf-8'
            )
            with self.assertRaisesRegex(ManifestError, 'invalid install transaction journal'):
                install_current_candidate()
            self.assertFalse((state_dir / 'transaction.json').exists())

            prior_id = prior_state.name
            original_artifact_sha256 = _hash_artifact(installed_app)
            prior_transaction_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'status': 'installing',
                        'candidate_id': prior_id,
                        'manifest_sha256': 'a' * 64,
                        'original_sha': original_sha,
                        'candidate_sha': candidate_sha,
                        'target': str(installed_app),
                        'staged': str(
                            installed_app.with_name(f'.Hermes.install-{prior_id}.app')
                        ),
                        'backup': str(
                            installed_app.with_name(f'.Hermes.rollback-{prior_id}.app')
                        ),
                        'original_artifact_sha256': original_artifact_sha256,
                        'candidate_artifact_sha256': verified.artifact_sha256,
                        'started_at': 123,
                    }
                ),
                encoding='utf-8',
            )
            self.assertFalse((state_dir / 'transaction.json').exists())
            before_head = git(active, 'rev-parse', 'HEAD')
            before_app_hash = _hash_artifact(installed_app)

            with self.assertRaisesRegex(ManifestError, 'manifest does not match'):
                install_current_candidate()
            self.assertFalse((state_dir / 'transaction.json').exists())

            prior_recovery_path = prior_state / 'recovery.json'
            prior_recovery_path.write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ManifestError, 'invalid install recovery report'):
                install_current_candidate()
            self.assertFalse((state_dir / 'transaction.json').exists())

            prior_recovery_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'status': 'rolled-back',
                        'candidate_id': prior_id,
                        'original_sha': original_sha,
                        'candidate_sha': candidate_sha,
                        'original_artifact_sha256': original_artifact_sha256,
                        'candidate_artifact_sha256': verified.artifact_sha256,
                        'recovered_at': 123,
                    }
                ),
                encoding='utf-8',
            )
            prior_install_path = prior_state / 'install.json'
            prior_install_path.write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ManifestError, 'not uniquely terminal'):
                install_current_candidate()
            prior_install_path.unlink()

            self.assertEqual(git(active, 'rev-parse', 'HEAD'), before_head)
            self.assertEqual(_hash_artifact(installed_app), before_app_hash)
            self.assertFalse((state_dir / 'transaction.json').exists())
            with (
                patch.object(coordinator.time, 'time', return_value=123),
                patch.object(coordinator.time, 'time_ns', return_value=123),
            ):
                installed = install_current_candidate()

            self.assertEqual(installed.status, 'installed')
            transaction = json.loads(
                (state_dir / 'transaction.json').read_text(encoding='utf-8')
            )
            self.assertGreater(transaction['started_at'], 123)
            self.assertEqual(git(active, 'rev-parse', 'HEAD'), candidate_sha)
            self.assertEqual((installed_app / 'marker.txt').read_text(encoding='utf-8'), 'new\n')
            self.assertEqual(git(active, 'rev-parse', installed.safety_ref), original_sha)
            self.assertTrue(installed.backup_path.exists())
            self.assertEqual(
                (installed.backup_path / 'marker.txt').read_text(encoding='utf-8'),
                'old\n',
            )
            transaction_path = state_dir / 'transaction.json'
            transaction = json.loads(transaction_path.read_text(encoding='utf-8'))
            self.assertEqual(transaction['status'], 'installing')

            transaction_without_started_at = dict(transaction)
            transaction_without_started_at.pop('started_at')
            transaction_path.chmod(0o600)
            transaction_path.write_text(
                json.dumps(transaction_without_started_at), encoding='utf-8'
            )
            with self.assertRaisesRegex(ManifestError, 'started_at'):
                recover_install_transaction(manifest, state_dir)
            transaction_path.write_text(json.dumps(transaction), encoding='utf-8')

            install_report_path = state_dir / 'install.json'
            install_report = json.loads(install_report_path.read_text(encoding='utf-8'))
            install_report_path.chmod(0o600)
            invalid_safety_report = dict(install_report)
            invalid_safety_report['safety_ref'] = 'refs/hermes-managed-update/safety/wrong'
            install_report_path.write_text(
                json.dumps(invalid_safety_report), encoding='utf-8'
            )
            with self.assertRaisesRegex(ManifestError, 'invalid committed install report'):
                recover_install_transaction(manifest, state_dir)
            install_report_path.write_text(json.dumps(install_report), encoding='utf-8')

            git(active, 'update-ref', installed.safety_ref, candidate_sha)
            with self.assertRaisesRegex(ManifestError, 'safety ref'):
                recover_install_transaction(manifest, state_dir)
            git(active, 'update-ref', installed.safety_ref, original_sha)

            self.assertEqual(
                recover_install_transaction(manifest, state_dir).resolve(),
                (state_dir / 'install.json').resolve(),
            )
            self.assertEqual(git(active, 'rev-parse', 'HEAD'), candidate_sha)
            self.assertEqual((installed_app / 'marker.txt').read_text(encoding='utf-8'), 'new\n')

            recovery_report = {
                'schema': 1,
                'status': 'rolled-back',
                'candidate_id': candidate.candidate_id,
                'original_sha': transaction['original_sha'],
                'candidate_sha': transaction['candidate_sha'],
                'original_artifact_sha256': transaction['original_artifact_sha256'],
                'candidate_artifact_sha256': transaction['candidate_artifact_sha256'],
                'recovered_at': 1,
            }
            (state_dir / 'recovery.json').write_text(
                json.dumps(recovery_report), encoding='utf-8'
            )
            with self.assertRaisesRegex(
                ManifestError, 'install transaction is not uniquely terminal'
            ):
                recover_install_transaction(manifest, state_dir)


class InstallRecoveryTests(unittest.TestCase):
    def test_recovery_holds_repository_lock_while_discovering_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            active = root / 'active'
            state_dir = root / 'state' / 'candidate-lock-probe'
            active.mkdir()
            state_dir.mkdir(parents=True)
            git(active, 'init', '-b', 'feat/longer-stable-v2')
            git(active, 'config', 'user.name', 'Managed Update Test')
            git(active, 'config', 'user.email', 'managed-update@example.invalid')
            (active / 'base.txt').write_text('base\n', encoding='utf-8')
            git(active, 'add', 'base.txt')
            git(active, 'commit', '-m', 'base')
            manifest = ManagedUpdateManifest(
                schema=1,
                mode='managed-patch-stack',
                worktree=active,
                branch='feat/longer-stable-v2',
                upstream='upstream/main',
                installed_app=root / 'Applications' / 'Hermes.app',
                features=(),
                verification_commands=(),
                artifact=None,
            )
            (state_dir / 'transaction.json').write_text('{}', encoding='utf-8')
            common_dir = Path(
                git(active, 'rev-parse', '--path-format=absolute', '--git-common-dir')
            )
            lock_path = common_dir / 'hermes-managed-update.lock'
            original_read_json_object = coordinator._read_json_object

            def probe_lock(path: Path) -> dict[str, object]:
                if Path(path).resolve() == (state_dir / 'transaction.json').resolve():
                    probe = subprocess.run(
                        [
                            sys.executable,
                            '-c',
                            (
                                'import fcntl, os, sys; '
                                'fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT, 0o600); '
                                'result=0; '
                                '\ntry: fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)'
                                '\nexcept BlockingIOError: result=75'
                                '\nos.close(fd); sys.exit(result)'
                            ),
                            str(lock_path),
                        ],
                        check=False,
                    )
                    self.assertEqual(
                        probe.returncode,
                        75,
                        'transaction discovery ran outside the repository update lock',
                    )
                    raise RuntimeError('lock probe complete')
                return original_read_json_object(path)

            with patch.object(coordinator, '_read_json_object', side_effect=probe_lock):
                with self.assertRaisesRegex(RuntimeError, 'lock probe complete'):
                    recover_pending_install_transactions(manifest, state_dir.parent)

    def test_pending_recovery_ignores_current_manifest_after_valid_rollback_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            active = root / 'active'
            target = root / 'Applications' / 'Hermes.app'
            state_root = root / 'state'
            state_dir = state_root / 'candidate-rolled-back'
            active.mkdir()
            target.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            (target / 'marker.txt').write_text('old\n', encoding='utf-8')

            git(active, 'init', '-b', 'feat/longer-stable-v2')
            git(active, 'config', 'user.name', 'Managed Update Test')
            git(active, 'config', 'user.email', 'managed-update@example.invalid')
            (active / 'version.txt').write_text('old\n', encoding='utf-8')
            git(active, 'add', 'version.txt')
            git(active, 'commit', '-m', 'original')
            original_sha = git(active, 'rev-parse', 'HEAD')
            (active / 'version.txt').write_text('new\n', encoding='utf-8')
            git(active, 'commit', '-am', 'candidate')
            candidate_sha = git(active, 'rev-parse', 'HEAD')
            git(active, 'reset', '--hard', original_sha)

            manifest = ManagedUpdateManifest(
                schema=1,
                mode='managed-patch-stack',
                worktree=active,
                branch='feat/longer-stable-v2',
                upstream='upstream/main',
                installed_app=target,
                features=(),
                verification_commands=(),
                artifact=None,
            )
            original_hash = _hash_artifact(target)
            candidate_bundle = root / 'candidate.app'
            candidate_bundle.mkdir()
            (candidate_bundle / 'marker.txt').write_text('new\n', encoding='utf-8')
            candidate_hash = _hash_artifact(candidate_bundle)
            shutil.rmtree(candidate_bundle)

            transaction = {
                'schema': 1,
                'status': 'installing',
                'candidate_id': state_dir.name,
                'manifest_sha256': manifest_digest(manifest),
                'original_sha': original_sha,
                'candidate_sha': candidate_sha,
                'target': str(target),
                'staged': str(target.with_name(f'.Hermes.install-{state_dir.name}.app')),
                'backup': str(target.with_name(f'.Hermes.rollback-{state_dir.name}.app')),
                'original_artifact_sha256': original_hash,
                'candidate_artifact_sha256': candidate_hash,
                'started_at': 1,
            }
            (state_dir / 'transaction.json').write_text(
                json.dumps(transaction), encoding='utf-8'
            )
            recovery_report = {
                'schema': 1,
                'status': 'rolled-back',
                'candidate_id': state_dir.name,
                'original_sha': original_sha,
                'candidate_sha': candidate_sha,
                'original_artifact_sha256': original_hash,
                'candidate_artifact_sha256': candidate_hash,
                'recovered_at': 1,
            }
            recovery_path = state_dir / 'recovery.json'
            recovery_path.write_text(json.dumps(recovery_report), encoding='utf-8')

            evolved_manifest = replace(manifest, upstream='upstream/next')
            self.assertEqual(
                recover_pending_install_transactions(evolved_manifest, state_root),
                (),
            )
            with self.assertRaisesRegex(ManifestError, 'manifest does not match'):
                recover_install_transaction(evolved_manifest, state_dir)

            before_head = git(active, 'rev-parse', 'HEAD')
            before_target_hash = _hash_artifact(target)

            recovery_path.unlink()
            with self.assertRaisesRegex(ManifestError, 'manifest does not match'):
                recover_pending_install_transactions(evolved_manifest, state_root)

            malformed_recovery = dict(recovery_report)
            malformed_recovery['candidate_sha'] = 'f' * 40
            recovery_path.write_text(json.dumps(malformed_recovery), encoding='utf-8')
            with self.assertRaisesRegex(ManifestError, 'invalid install recovery report'):
                recover_pending_install_transactions(evolved_manifest, state_root)

            recovery_path.write_text(json.dumps(recovery_report), encoding='utf-8')
            (state_dir / 'install.json').write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ManifestError, 'not uniquely terminal'):
                recover_pending_install_transactions(evolved_manifest, state_root)

            self.assertEqual(git(active, 'rev-parse', 'HEAD'), before_head)
            self.assertEqual(_hash_artifact(target), before_target_hash)
            self.assertFalse(Path(transaction['staged']).exists())
            self.assertFalse(Path(transaction['backup']).exists())

    def test_recovery_treats_older_committed_install_as_historical(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            active = root / 'active'
            target = root / 'Applications' / 'Hermes.app'
            state_root = root / 'state'
            active.mkdir()
            target.mkdir(parents=True)
            state_root.mkdir()
            (target / 'marker.txt').write_text('second\n', encoding='utf-8')

            git(active, 'init', '-b', 'feat/longer-stable-v2')
            git(active, 'config', 'user.name', 'Managed Update Test')
            git(active, 'config', 'user.email', 'managed-update@example.invalid')
            (active / 'version.txt').write_text('original\n', encoding='utf-8')
            git(active, 'add', 'version.txt')
            git(active, 'commit', '-m', 'original')
            original_sha = git(active, 'rev-parse', 'HEAD')
            (active / 'version.txt').write_text('first\n', encoding='utf-8')
            git(active, 'commit', '-am', 'first candidate')
            first_sha = git(active, 'rev-parse', 'HEAD')
            (active / 'version.txt').write_text('second\n', encoding='utf-8')
            git(active, 'commit', '-am', 'second candidate')
            second_sha = git(active, 'rev-parse', 'HEAD')

            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(active),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(target),
                        'features': [],
                    }
                ),
                encoding='utf-8',
            )
            manifest = load_manifest(manifest_path)

            # Lexical order intentionally opposes chronological order. Recovery
            # must select by the immutable started_at field.
            first_state = state_root / 'candidate-z-historical'
            second_state = state_root / 'candidate-a-current'
            first_state.mkdir()
            second_state.mkdir()
            first_backup = target.with_name(f'.Hermes.rollback-{first_state.name}.app')
            second_backup = target.with_name(f'.Hermes.rollback-{second_state.name}.app')
            first_backup.mkdir()
            second_backup.mkdir()
            (first_backup / 'marker.txt').write_text('original\n', encoding='utf-8')
            (second_backup / 'marker.txt').write_text('first\n', encoding='utf-8')
            original_hash = _hash_artifact(first_backup)
            first_hash = _hash_artifact(second_backup)
            second_hash = _hash_artifact(target)

            def write_committed_transaction(
                state_dir: Path,
                *,
                started_at: int,
                original_commit: str,
                candidate_commit: str,
                original_artifact: str,
                candidate_artifact: str,
                backup: Path,
            ) -> None:
                candidate_id = state_dir.name
                (state_dir / 'transaction.json').write_text(
                    json.dumps(
                        {
                            'schema': 1,
                            'status': 'installing',
                            'candidate_id': candidate_id,
                            'manifest_sha256': manifest_digest(manifest),
                            'original_sha': original_commit,
                            'candidate_sha': candidate_commit,
                            'target': str(target),
                            'staged': str(
                                target.with_name(f'.Hermes.install-{candidate_id}.app')
                            ),
                            'backup': str(backup),
                            'original_artifact_sha256': original_artifact,
                            'candidate_artifact_sha256': candidate_artifact,
                            'started_at': started_at,
                        }
                    ),
                    encoding='utf-8',
                )
                (state_dir / 'install.json').write_text(
                    json.dumps(
                        {
                            'schema': 1,
                            'status': 'installed',
                            'candidate_id': candidate_id,
                            'original_sha': original_commit,
                            'candidate_sha': candidate_commit,
                            'artifact_sha256': candidate_artifact,
                            'safety_ref': f'refs/hermes-managed-update/safety/{candidate_id}',
                        }
                    ),
                    encoding='utf-8',
                )
                git(
                    active,
                    'update-ref',
                    f'refs/hermes-managed-update/safety/{candidate_id}',
                    original_commit,
                )

            write_committed_transaction(
                first_state,
                started_at=1,
                original_commit=original_sha,
                candidate_commit=first_sha,
                original_artifact=original_hash,
                candidate_artifact=first_hash,
                backup=first_backup,
            )
            write_committed_transaction(
                second_state,
                started_at=2,
                original_commit=first_sha,
                candidate_commit=second_sha,
                original_artifact=first_hash,
                candidate_artifact=second_hash,
                backup=second_backup,
            )

            # Historical transaction metadata remains immutable evidence, but
            # its old manifest/path and retired rollback bundle are not live
            # authority for the latest installed generation.
            historical_transaction_path = first_state / 'transaction.json'
            historical_transaction = json.loads(
                historical_transaction_path.read_text(encoding='utf-8')
            )
            retired_target = root / 'Retired Applications' / 'Hermes.app'
            historical_transaction.update(
                {
                    'manifest_sha256': 'a' * 64,
                    'target': str(retired_target),
                    'staged': str(
                        retired_target.with_name(
                            f'.Hermes.install-{first_state.name}.app'
                        )
                    ),
                    'backup': str(
                        retired_target.with_name(
                            f'.Hermes.rollback-{first_state.name}.app'
                        )
                    ),
                }
            )
            historical_transaction_path.write_text(
                json.dumps(historical_transaction), encoding='utf-8'
            )
            shutil.rmtree(first_backup)

            evolved_manifest = replace(manifest, upstream='upstream/next')
            self.assertEqual(
                recover_pending_install_transactions(evolved_manifest, state_root), ()
            )
            self.assertEqual(git(active, 'rev-parse', 'HEAD'), second_sha)
            self.assertEqual((target / 'marker.txt').read_text(encoding='utf-8'), 'second\n')
            with self.assertRaisesRegex(ManifestError, 'not the latest install transaction'):
                recover_install_transaction(manifest, first_state)

            baseline_historical_transaction = json.loads(
                historical_transaction_path.read_text(encoding='utf-8')
            )
            invalid_started_at_cases = (
                ('missing', None, 'started_at is invalid'),
                ('boolean', True, 'started_at is invalid'),
                ('negative', -1, 'started_at is invalid'),
                ('string', '1', 'started_at is invalid'),
                ('duplicate', 2, 'ambiguous started_at ordering'),
            )
            for case_name, invalid_value, expected_error in invalid_started_at_cases:
                with self.subTest(started_at=case_name):
                    invalid_transaction = dict(baseline_historical_transaction)
                    if case_name == 'missing':
                        invalid_transaction.pop('started_at')
                    else:
                        invalid_transaction['started_at'] = invalid_value
                    historical_transaction_path.write_text(
                        json.dumps(invalid_transaction), encoding='utf-8'
                    )
                    with self.assertRaisesRegex(ManifestError, expected_error):
                        recover_pending_install_transactions(manifest, state_root)
            historical_transaction_path.write_text(
                json.dumps(baseline_historical_transaction), encoding='utf-8'
            )

            historical_install_path = first_state / 'install.json'
            historical_install = json.loads(
                historical_install_path.read_text(encoding='utf-8')
            )
            historical_recovery_path = first_state / 'recovery.json'
            historical_recovery_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'status': 'rolled-back',
                        'candidate_id': first_state.name,
                        'original_sha': original_sha,
                        'candidate_sha': first_sha,
                        'original_artifact_sha256': original_hash,
                        'candidate_artifact_sha256': first_hash,
                        'recovered_at': 1,
                    }
                ),
                encoding='utf-8',
            )
            with self.assertRaisesRegex(
                ManifestError, 'historical install transaction is not uniquely terminal'
            ):
                recover_pending_install_transactions(manifest, state_root)
            historical_recovery_path.unlink()
            historical_install_path.unlink()
            with self.assertRaisesRegex(
                ManifestError, 'historical install transaction is not uniquely terminal'
            ):
                recover_pending_install_transactions(manifest, state_root)
            historical_install_path.write_text(
                json.dumps(historical_install), encoding='utf-8'
            )

            tampered_historical = json.loads(
                historical_install_path.read_text(encoding='utf-8')
            )
            tampered_historical['candidate_sha'] = 'f' * 40
            (first_state / 'install.json').write_text(
                json.dumps(tampered_historical), encoding='utf-8'
            )
            with self.assertRaisesRegex(
                ManifestError, 'invalid historical install transaction report'
            ):
                recover_pending_install_transactions(manifest, state_root)

    def test_recovery_rolls_back_git_only_and_app_exchanged_crash_states(self) -> None:
        for crash_state in ('git-promoted', 'app-exchanged', 'unrelated-head'):
            with self.subTest(crash_state=crash_state), tempfile.TemporaryDirectory() as raw_temp:
                root = Path(raw_temp)
                active = root / 'active'
                target = root / 'Applications' / 'Hermes.app'
                state_dir = root / 'state' / 'candidate-recovery'
                staged = target.with_name('.Hermes.install-candidate-recovery.app')
                backup = target.with_name('.Hermes.rollback-candidate-recovery.app')
                active.mkdir()
                target.mkdir(parents=True)
                state_dir.mkdir(parents=True)
                (target / 'marker.txt').write_text('old\n', encoding='utf-8')
                git(active, 'init', '-b', 'feat/longer-stable-v2')
                git(active, 'config', 'user.name', 'Managed Update Test')
                git(active, 'config', 'user.email', 'managed-update@example.invalid')
                (active / 'base.txt').write_text('old\n', encoding='utf-8')
                git(active, 'add', 'base.txt')
                git(active, 'commit', '-m', 'original')
                original_sha = git(active, 'rev-parse', 'HEAD')
                (active / 'candidate.txt').write_text('new\n', encoding='utf-8')
                git(active, 'add', 'candidate.txt')
                git(active, 'commit', '-m', 'candidate')
                candidate_sha = git(active, 'rev-parse', 'HEAD')
                unrelated_sha = None
                if crash_state == 'unrelated-head':
                    (active / 'unrelated.txt').write_text('unrelated\n', encoding='utf-8')
                    git(active, 'add', 'unrelated.txt')
                    git(active, 'commit', '-m', 'unrelated')
                    unrelated_sha = git(active, 'rev-parse', 'HEAD')
                manifest_path = root / 'manifest.json'
                manifest_path.write_text(
                    json.dumps(
                        {
                            'schema': 1,
                            'mode': 'managed-patch-stack',
                            'worktree': str(active),
                            'branch': 'feat/longer-stable-v2',
                            'upstream': 'upstream/main',
                            'installed_app': str(target),
                            'features': [],
                        }
                    ),
                    encoding='utf-8',
                )
                manifest = load_manifest(manifest_path)
                old_hash = _hash_artifact(target)
                candidate_bundle = root / 'candidate.app'
                candidate_bundle.mkdir()
                (candidate_bundle / 'marker.txt').write_text('new\n', encoding='utf-8')
                new_hash = _hash_artifact(candidate_bundle)

                if crash_state == 'git-promoted':
                    shutil.copytree(candidate_bundle, staged)
                else:
                    shutil.rmtree(target)
                    shutil.copytree(candidate_bundle, target)
                    backup.mkdir()
                    (backup / 'marker.txt').write_text('old\n', encoding='utf-8')

                (state_dir / 'transaction.json').write_text(
                    json.dumps(
                        {
                            'schema': 1,
                            'status': 'installing',
                            'candidate_id': 'candidate-recovery',
                            'manifest_sha256': manifest_digest(manifest),
                            'original_sha': original_sha,
                            'candidate_sha': candidate_sha,
                            'target': str(target),
                            'staged': str(staged),
                            'backup': str(backup),
                            'original_artifact_sha256': old_hash,
                            'candidate_artifact_sha256': new_hash,
                            'started_at': 1,
                        }
                    ),
                    encoding='utf-8',
                )

                if crash_state == 'unrelated-head':
                    before = {
                        path: _hash_artifact(path) if path.exists() else None
                        for path in (target, staged, backup)
                    }
                    with self.assertRaisesRegex(
                        ManifestError,
                        'active Git HEAD matches neither side of the install transaction',
                    ):
                        recover_pending_install_transactions(manifest, root / 'state')
                    self.assertEqual(git(active, 'rev-parse', 'HEAD'), unrelated_sha)
                    self.assertEqual(
                        before,
                        {
                            path: _hash_artifact(path) if path.exists() else None
                            for path in (target, staged, backup)
                        },
                    )
                    self.assertFalse((state_dir / 'recovery.json').exists())
                    continue

                reports = recover_pending_install_transactions(manifest, root / 'state')
                self.assertEqual(len(reports), 1)
                report = reports[0]
                self.assertEqual(recover_pending_install_transactions(manifest, root / 'state'), ())

                self.assertEqual(git(active, 'rev-parse', 'HEAD'), original_sha)
                self.assertEqual((target / 'marker.txt').read_text(encoding='utf-8'), 'old\n')
                self.assertFalse(staged.exists())
                self.assertFalse(backup.exists())
                self.assertEqual(json.loads(report.read_text(encoding='utf-8'))['status'], 'rolled-back')

                recovered = json.loads(report.read_text(encoding='utf-8'))
                invalid_recovered_at = dict(recovered)
                invalid_recovered_at['recovered_at'] = '1'
                report.chmod(0o600)
                report.write_text(json.dumps(invalid_recovered_at), encoding='utf-8')
                with self.assertRaisesRegex(ManifestError, 'invalid install recovery report'):
                    recover_pending_install_transactions(manifest, root / 'state')
                report.write_text(json.dumps(recovered), encoding='utf-8')

                tampered = dict(recovered)
                tampered['candidate_sha'] = 'f' * 40
                report.write_text(json.dumps(tampered), encoding='utf-8')
                with self.assertRaisesRegex(ManifestError, 'invalid install recovery report'):
                    recover_pending_install_transactions(manifest, root / 'state')


class CoordinatorCliTests(unittest.TestCase):
    def test_check_emits_machine_readable_repository_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            active = root / 'active'
            installed_app = root / 'Hermes.app'
            active.mkdir()
            installed_app.mkdir()
            git(active, 'init', '-b', 'feat/longer-stable-v2')
            git(active, 'config', 'user.name', 'Managed Update Test')
            git(active, 'config', 'user.email', 'managed-update@example.invalid')
            (active / 'base.txt').write_text('base\n', encoding='utf-8')
            git(active, 'add', 'base.txt')
            git(active, 'commit', '-m', 'base')
            base_sha = git(active, 'rev-parse', 'HEAD')
            git(active, 'update-ref', 'refs/remotes/upstream/main', base_sha)
            (active / 'custom.txt').write_text('custom\n', encoding='utf-8')
            git(active, 'add', 'custom.txt')
            git(active, 'commit', '-m', 'feat: custom behavior')
            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(active),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(installed_app),
                        'features': [],
                    }
                ),
                encoding='utf-8',
            )
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main(
                    ['--manifest', str(manifest_path), 'check', '--no-fetch']
                )

            self.assertEqual(exit_code, 0)
            event = json.loads(output.getvalue())
            self.assertEqual(event['event'], 'check-complete')
            self.assertEqual(event['branch'], 'feat/longer-stable-v2')
            self.assertEqual(event['ahead'], 1)
            self.assertEqual(event['behind'], 0)
            self.assertEqual(event['custom_commits'], 1)

    def test_prepare_emits_verified_candidate_and_confirmation_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            active = root / 'active'
            installed_app = root / 'Hermes.app'
            active.mkdir()
            installed_app.mkdir()
            git(active, 'init', '-b', 'feat/longer-stable-v2')
            git(active, 'config', 'user.name', 'Managed Update Test')
            git(active, 'config', 'user.email', 'managed-update@example.invalid')
            (active / 'base.txt').write_text('base\n', encoding='utf-8')
            git(active, 'add', 'base.txt')
            git(active, 'commit', '-m', 'base')
            base_sha = git(active, 'rev-parse', 'HEAD')
            git(active, 'update-ref', 'refs/remotes/upstream/main', base_sha)
            (active / 'custom.txt').write_text('custom\n', encoding='utf-8')
            git(active, 'add', 'custom.txt')
            git(active, 'commit', '-m', 'feat: custom behavior')
            original_sha = git(active, 'rev-parse', 'HEAD')
            git(active, 'switch', '-c', 'upstream-line', base_sha)
            (active / 'upstream.txt').write_text('upstream\n', encoding='utf-8')
            git(active, 'add', 'upstream.txt')
            git(active, 'commit', '-m', 'upstream change')
            upstream_sha = git(active, 'rev-parse', 'HEAD')
            git(active, 'update-ref', 'refs/remotes/upstream/main', upstream_sha)
            git(active, 'switch', 'feat/longer-stable-v2')
            self.assertEqual(git(active, 'rev-parse', 'HEAD'), original_sha)

            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(active),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(installed_app),
                        'features': [],
                        'verification_commands': [
                            {
                                'name': 'build',
                                'argv': [
                                    sys.executable,
                                    '-c',
                                    "from pathlib import Path; Path('artifact.bin').write_bytes(b'cli')",
                                ],
                                'cwd': '.',
                            }
                        ],
                        'artifact': 'artifact.bin',
                    }
                ),
                encoding='utf-8',
            )
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main(
                    [
                        '--manifest',
                        str(manifest_path),
                        'prepare',
                        '--state-root',
                        str(root / 'state'),
                        '--candidate-id',
                        'cli-candidate',
                        '--no-fetch',
                    ]
                )

            self.assertEqual(exit_code, 0)
            event = json.loads(output.getvalue())
            self.assertEqual(event['event'], 'candidate-ready')
            self.assertEqual(event['candidate_id'], 'cli-candidate')
            self.assertEqual(event['status'], 'verified')
            self.assertEqual(len(event['confirmation_token']), 64)
            self.assertEqual(event['artifact_sha256'], hashlib.sha256(b'cli').hexdigest())
            self.assertEqual(git(active, 'rev-parse', 'HEAD'), original_sha)

    def test_accept_review_loads_candidate_and_emits_verified_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            active = root / 'active'
            installed_app = root / 'Hermes.app'
            active.mkdir()
            installed_app.mkdir()
            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(active),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(installed_app),
                        'features': [],
                    }
                ),
                encoding='utf-8',
            )
            state_dir = root / 'state' / 'cli-review'
            candidate = CandidateResult(
                candidate_id='cli-review',
                state_dir=state_dir,
                worktree=state_dir / 'worktree',
                status='review',
                original_sha='a' * 40,
                upstream_sha='b' * 40,
                candidate_sha='c' * 40,
                conflicts=(),
                original_commit_subjects=(),
                candidate_commit_subjects=(),
                recommendations=(),
            )
            verified = VerifiedCandidate(
                candidate_id='cli-review',
                status='verified',
                artifact=state_dir / 'worktree' / 'Hermes.app',
                artifact_sha256='d' * 64,
                manifest_sha256='e' * 64,
                decision_sha256=None,
                commands=(),
                approval_token_sha256=hashlib.sha256(('f' * 64).encode('ascii')).hexdigest(),
                confirmation_token='f' * 64,
            )
            output = io.StringIO()

            with (
                patch('scripts.managed_update_coordinator.load_candidate', return_value=candidate),
                patch(
                    'scripts.managed_update_coordinator.accept_candidate_review',
                    return_value=verified,
                ) as accept_mock,
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        '--manifest',
                        str(manifest_path),
                        'accept-review',
                        '--state-root',
                        str(root / 'state'),
                        '--candidate-id',
                        'cli-review',
                    ]
                )

            self.assertEqual(exit_code, 0)
            event = json.loads(output.getvalue())
            self.assertEqual(event['event'], 'candidate-ready')
            self.assertEqual(event['candidate_id'], 'cli-review')
            self.assertEqual(event['status'], 'verified')
            self.assertEqual(len(event['confirmation_token']), 64)
            accept_mock.assert_called_once()

    def test_install_loads_persisted_candidate_and_emits_installed_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp)
            active = root / 'active'
            installed_app = root / 'Hermes.app'
            active.mkdir()
            installed_app.mkdir()
            manifest_path = root / 'manifest.json'
            manifest_path.write_text(
                json.dumps(
                    {
                        'schema': 1,
                        'mode': 'managed-patch-stack',
                        'worktree': str(active),
                        'branch': 'feat/longer-stable-v2',
                        'upstream': 'upstream/main',
                        'installed_app': str(installed_app),
                        'features': [],
                    }
                ),
                encoding='utf-8',
            )
            state_dir = root / 'state' / 'cli-install'
            candidate = CandidateResult(
                candidate_id='cli-install',
                state_dir=state_dir,
                worktree=state_dir / 'worktree',
                status='ready',
                original_sha='a' * 40,
                upstream_sha='b' * 40,
                candidate_sha='c' * 40,
                conflicts=(),
                original_commit_subjects=(),
                candidate_commit_subjects=(),
                recommendations=(),
            )
            verified = VerifiedCandidate(
                candidate_id='cli-install',
                status='verified',
                artifact=state_dir / 'worktree' / 'Hermes.app',
                artifact_sha256='d' * 64,
                manifest_sha256='e' * 64,
                decision_sha256=None,
                commands=(),
                approval_token_sha256=hashlib.sha256(('f' * 64).encode('ascii')).hexdigest(),
                verification_report_sha256='1' * 64,
            )
            install_result = InstallResult(
                candidate_id='cli-install',
                status='installed',
                safety_ref='refs/hermes-managed-update/safety/cli-install',
                backup_path=root / '.Hermes.rollback-cli-install.app',
            )
            output = io.StringIO()

            with (
                patch(
                    'scripts.managed_update_coordinator.load_candidate',
                    return_value=candidate,
                ),
                patch(
                    'scripts.managed_update_coordinator.load_verified_candidate',
                    return_value=verified,
                ),
                patch(
                    'scripts.managed_update_coordinator.install_verified_candidate',
                    return_value=install_result,
                ) as install_mock,
                patch(
                    'scripts.managed_update_coordinator._read_install_approval_fd',
                    return_value=(
                        'f' * 64,
                        candidate.candidate_sha,
                        verified.artifact_sha256,
                        verified.verification_report_sha256,
                    ),
                ),
                patch('scripts.managed_update_coordinator.validate_install_approval'),
                patch(
                    'scripts.managed_update_coordinator._hash_artifact',
                    return_value=verified.artifact_sha256,
                ) as artifact_hash_mock,
                patch('scripts.managed_update_coordinator._write_installer_ready_fd'),
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        '--manifest',
                        str(manifest_path),
                        'install',
                        '--state-root',
                        str(root / 'state'),
                        '--candidate-id',
                        'cli-install',
                        '--approval-fd',
                        '3',
                        '--ready-fd',
                        '4',
                    ]
                )

            self.assertEqual(exit_code, 0)
            event = json.loads(output.getvalue())
            self.assertEqual(event['event'], 'install-complete')
            self.assertEqual(event['candidate_id'], 'cli-install')
            self.assertEqual(event['status'], 'installed')
            self.assertEqual(event['safety_ref'], install_result.safety_ref)
            artifact_hash_mock.assert_called_once_with(verified.artifact)
            self.assertEqual(
                install_mock.call_args.kwargs['confirmation_token'],
                'f' * 64,
            )

            tampered_output = io.StringIO()
            with (
                patch(
                    'scripts.managed_update_coordinator.load_candidate',
                    return_value=candidate,
                ),
                patch(
                    'scripts.managed_update_coordinator.load_verified_candidate',
                    return_value=verified,
                ),
                patch(
                    'scripts.managed_update_coordinator.install_verified_candidate',
                    return_value=install_result,
                ) as rejected_install,
                patch(
                    'scripts.managed_update_coordinator._read_install_approval_fd',
                    return_value=(
                        'f' * 64,
                        candidate.candidate_sha,
                        verified.artifact_sha256,
                        '2' * 64,
                    ),
                ),
                patch('scripts.managed_update_coordinator._write_installer_ready_fd') as ready_mock,
                redirect_stdout(tampered_output),
            ):
                rejected_exit_code = main(
                    [
                        '--manifest',
                        str(manifest_path),
                        'install',
                        '--state-root',
                        str(root / 'state'),
                        '--candidate-id',
                        'cli-install',
                        '--approval-fd',
                        '3',
                        '--ready-fd',
                        '4',
                    ]
                )

            self.assertEqual(rejected_exit_code, 1)
            rejected_install.assert_not_called()
            ready_mock.assert_not_called()
            self.assertIn('verification report was replaced', tampered_output.getvalue())

            changed_artifact_output = io.StringIO()
            with (
                patch(
                    'scripts.managed_update_coordinator.load_candidate',
                    return_value=candidate,
                ),
                patch(
                    'scripts.managed_update_coordinator.load_verified_candidate',
                    return_value=verified,
                ),
                patch(
                    'scripts.managed_update_coordinator.install_verified_candidate',
                    return_value=install_result,
                ) as changed_artifact_install,
                patch(
                    'scripts.managed_update_coordinator._read_install_approval_fd',
                    return_value=(
                        'f' * 64,
                        candidate.candidate_sha,
                        verified.artifact_sha256,
                        verified.verification_report_sha256,
                    ),
                ),
                patch('scripts.managed_update_coordinator.validate_install_approval'),
                patch(
                    'scripts.managed_update_coordinator._hash_artifact',
                    return_value='9' * 64,
                ),
                patch('scripts.managed_update_coordinator._write_installer_ready_fd') as changed_ready_mock,
                redirect_stdout(changed_artifact_output),
            ):
                changed_artifact_exit_code = main(
                    [
                        '--manifest',
                        str(manifest_path),
                        'install',
                        '--state-root',
                        str(root / 'state'),
                        '--candidate-id',
                        'cli-install',
                        '--approval-fd',
                        '3',
                        '--ready-fd',
                        '4',
                    ]
                )

            self.assertEqual(changed_artifact_exit_code, 1)
            changed_artifact_install.assert_not_called()
            changed_ready_mock.assert_not_called()
            self.assertIn('artifact changed before installer readiness', changed_artifact_output.getvalue())


if __name__ == '__main__':
    unittest.main()
