from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, field, replace
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import select
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Callable


SUPPORTED_SCHEMA = 1
SUPPORTED_MODE = 'managed-patch-stack'


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ManagedFeature:
    id: str
    commit_subject: str


@dataclass(frozen=True)
class VerificationCommand:
    name: str
    argv: tuple[str, ...]
    cwd: str


@dataclass(frozen=True)
class ManagedUpdateManifest:
    schema: int
    mode: str
    worktree: Path
    branch: str
    upstream: str
    installed_app: Path
    features: tuple[ManagedFeature, ...]
    verification_commands: tuple[VerificationCommand, ...]
    artifact: str | None


@dataclass(frozen=True)
class CommitIdentity:
    sha: str
    subject: str
    patch_id: str


@dataclass(frozen=True)
class RepositoryStatus:
    current_branch: str
    original_sha: str
    upstream_sha: str
    ahead: int
    behind: int
    clean: bool
    custom_commit_subjects: tuple[str, ...]
    custom_commits: tuple[CommitIdentity, ...] = ()


@dataclass(frozen=True)
class OptimizationRecommendation:
    feature_id: str
    kind: str
    commit_subject: str


@dataclass(frozen=True)
class CandidateResult:
    candidate_id: str
    state_dir: Path
    worktree: Path
    status: str
    original_sha: str
    upstream_sha: str
    candidate_sha: str
    conflicts: tuple[str, ...]
    original_commit_subjects: tuple[str, ...]
    candidate_commit_subjects: tuple[str, ...]
    recommendations: tuple[OptimizationRecommendation, ...]
    original_commits: tuple[CommitIdentity, ...] = ()
    candidate_commits: tuple[CommitIdentity, ...] = ()


@dataclass(frozen=True)
class VerificationCommandResult:
    name: str
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class VerifiedCandidate:
    candidate_id: str
    status: str
    artifact: Path
    artifact_sha256: str
    manifest_sha256: str
    decision_sha256: str | None
    commands: tuple[VerificationCommandResult, ...]
    approval_token_sha256: str
    verification_report_sha256: str = field(default='', compare=False)
    confirmation_token: str = field(default='', compare=False, repr=False)


@dataclass(frozen=True)
class InstallResult:
    candidate_id: str
    status: str
    safety_ref: str
    backup_path: Path


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f'{key} must be a non-empty string')
    return value.strip()


def _required_git_ref(data: dict[str, Any], key: str) -> str:
    value = _required_string(data, key)
    if value.startswith('-'):
        raise ManifestError(f'{key} must be a valid Git ref')
    checked = subprocess.run(
        ['git', 'check-ref-format', '--branch', value],
        check=False,
        capture_output=True,
        text=True,
    )
    if checked.returncode != 0:
        raise ManifestError(f'{key} must be a valid Git ref')
    return value


def load_manifest(path: Path) -> ManagedUpdateManifest:
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f'cannot read managed update manifest: {error}') from error

    if not isinstance(raw, dict):
        raise ManifestError('manifest root must be an object')
    if raw.get('schema') != SUPPORTED_SCHEMA:
        raise ManifestError(f'manifest schema must be {SUPPORTED_SCHEMA}')
    if raw.get('mode') != SUPPORTED_MODE:
        raise ManifestError(f'manifest mode must be {SUPPORTED_MODE}')

    feature_rows = raw.get('features', [])
    if not isinstance(feature_rows, list):
        raise ManifestError('features must be an array')

    features: list[ManagedFeature] = []
    for row in feature_rows:
        if not isinstance(row, dict):
            raise ManifestError('each feature must be an object')
        features.append(
            ManagedFeature(
                id=_required_string(row, 'id'),
                commit_subject=_required_string(row, 'commit_subject'),
            )
        )

    verification_rows = raw.get('verification_commands', [])
    if not isinstance(verification_rows, list):
        raise ManifestError('verification_commands must be an array')
    verification_commands: list[VerificationCommand] = []
    for row in verification_rows:
        if not isinstance(row, dict):
            raise ManifestError('each verification command must be an object')
        argv = row.get('argv')
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(value, str) or not value for value in argv)
        ):
            raise ManifestError('verification command argv must be a non-empty string array')
        cwd = row.get('cwd', '.')
        if not isinstance(cwd, str) or not cwd:
            raise ManifestError('verification command cwd must be a non-empty string')
        verification_commands.append(
            VerificationCommand(
                name=_required_string(row, 'name'),
                argv=tuple(argv),
                cwd=cwd,
            )
        )

    artifact = raw.get('artifact')
    if artifact is not None and (not isinstance(artifact, str) or not artifact):
        raise ManifestError('artifact must be a non-empty string when configured')

    return ManagedUpdateManifest(
        schema=SUPPORTED_SCHEMA,
        mode=SUPPORTED_MODE,
        worktree=Path(_required_string(raw, 'worktree')).expanduser().resolve(),
        branch=_required_git_ref(raw, 'branch'),
        upstream=_required_git_ref(raw, 'upstream'),
        installed_app=Path(_required_string(raw, 'installed_app')).expanduser().resolve(),
        features=tuple(features),
        verification_commands=tuple(verification_commands),
        artifact=artifact,
    )


def manifest_digest(manifest: ManagedUpdateManifest) -> str:
    payload = {
        'schema': manifest.schema,
        'mode': manifest.mode,
        'worktree': str(manifest.worktree),
        'branch': manifest.branch,
        'upstream': manifest.upstream,
        'installed_app': str(manifest.installed_app),
        'features': [
            {'id': feature.id, 'commit_subject': feature.commit_subject}
            for feature in manifest.features
        ],
        'verification_commands': [
            {'name': command.name, 'argv': list(command.argv), 'cwd': command.cwd}
            for command in manifest.verification_commands
        ],
        'artifact': manifest.artifact,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _run_git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ['git', *args],
        cwd=worktree,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f'exit {result.returncode}'
        raise ManifestError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _commit_patch_id(worktree: Path, commit_sha: str) -> str:
    patch = subprocess.run(
        ['git', 'show', '--pretty=format:', '--binary', commit_sha],
        cwd=worktree,
        check=False,
        capture_output=True,
    )
    if patch.returncode != 0:
        detail = patch.stderr.decode('utf-8', errors='replace').strip() or f'exit {patch.returncode}'
        raise ManifestError(f'cannot render patch for commit {commit_sha}: {detail}')
    identified = subprocess.run(
        ['git', 'patch-id', '--stable'],
        cwd=worktree,
        input=patch.stdout,
        check=False,
        capture_output=True,
    )
    if identified.returncode != 0:
        detail = identified.stderr.decode('utf-8', errors='replace').strip() or f'exit {identified.returncode}'
        raise ManifestError(f'cannot calculate patch-id for commit {commit_sha}: {detail}')
    fields = identified.stdout.decode('ascii', errors='strict').strip().split()
    if not fields:
        return ''
    patch_id = fields[0]
    if not re.fullmatch(r'[0-9a-f]{40,64}', patch_id):
        raise ManifestError(f'git returned an invalid patch-id for commit {commit_sha}')
    return patch_id


def _commit_identities(worktree: Path, base_sha: str, head_sha: str) -> tuple[CommitIdentity, ...]:
    commits_raw = _run_git(worktree, 'rev-list', '--reverse', f'{base_sha}..{head_sha}')
    commits: list[CommitIdentity] = []
    for commit_sha in commits_raw.splitlines():
        if not re.fullmatch(r'[0-9a-f]{40}', commit_sha):
            raise ManifestError('git returned an invalid commit identity')
        commits.append(
            CommitIdentity(
                sha=commit_sha,
                subject=_run_git(worktree, 'show', '-s', '--format=%s', commit_sha),
                patch_id=_commit_patch_id(worktree, commit_sha),
            )
        )
    return tuple(commits)


def _upstream_equivalent_commit_shas(
    worktree: Path,
    upstream_sha: str,
    original_sha: str,
) -> frozenset[str]:
    cherry = _run_git(worktree, 'cherry', upstream_sha, original_sha)
    patch_equivalent: set[str] = set()
    for line in cherry.splitlines():
        fields = line.split()
        if (
            len(fields) != 2
            or fields[0] not in {'+', '-'}
            or not re.fullmatch(r'[0-9a-f]{40}', fields[1])
        ):
            raise ManifestError('git cherry returned malformed patch identity output')
        if fields[0] == '-':
            patch_equivalent.add(fields[1])
    if not patch_equivalent:
        return frozenset()

    # Stable patch IDs deliberately ignore whitespace. Use them only as an
    # inexpensive candidate filter: whitespace is semantic in Python/YAML and
    # must never be enough to silently drop a local feature. Replay every
    # alleged equivalent independently onto the pinned upstream commit and
    # accept it only if the replay leaves index and worktree byte-identical.
    probe_root = Path(tempfile.mkdtemp(prefix='hermes-managed-equivalence-'))
    probe = probe_root / 'worktree'
    strict: set[str] = set()
    added = False
    try:
        _run_git(worktree, 'worktree', 'add', '--detach', str(probe), upstream_sha)
        added = True
        for commit_sha in sorted(patch_equivalent):
            _run_git(probe, 'reset', '--hard', upstream_sha)
            subprocess.run(
                ['git', 'cherry-pick', '--abort'],
                cwd=probe,
                check=False,
                capture_output=True,
            )
            replay = subprocess.run(
                [
                    'git',
                    '-c',
                    'core.hooksPath=/dev/null',
                    'cherry-pick',
                    '--no-commit',
                    commit_sha,
                ],
                cwd=probe,
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, 'GIT_EDITOR': 'true'},
            )
            cherry_pick_head = subprocess.run(
                ['git', 'rev-parse', '--quiet', '--verify', 'CHERRY_PICK_HEAD'],
                cwd=probe,
                check=False,
                capture_output=True,
            ).returncode == 0
            unmerged = _run_git(probe, 'diff', '--name-only', '--diff-filter=U')
            index_clean = subprocess.run(
                ['git', 'diff', '--cached', '--quiet', '--exit-code'],
                cwd=probe,
                check=False,
            ).returncode == 0
            worktree_clean = subprocess.run(
                ['git', 'diff', '--quiet', '--exit-code'],
                cwd=probe,
                check=False,
            ).returncode == 0
            if (
                not unmerged
                and index_clean
                and worktree_clean
                and (replay.returncode == 0 or cherry_pick_head)
            ):
                strict.add(commit_sha)
    finally:
        if added:
            subprocess.run(
                ['git', 'worktree', 'remove', '--force', str(probe)],
                cwd=worktree,
                check=False,
                capture_output=True,
            )
        shutil.rmtree(probe_root, ignore_errors=True)
    return frozenset(strict)


def inspect_repository(manifest: ManagedUpdateManifest) -> RepositoryStatus:
    current_branch = _run_git(manifest.worktree, 'branch', '--show-current')
    original_sha = _run_git(manifest.worktree, 'rev-parse', 'HEAD^{commit}')
    upstream_sha = _run_git(manifest.worktree, 'rev-parse', f'{manifest.upstream}^{{commit}}')
    ahead = int(_run_git(manifest.worktree, 'rev-list', '--count', f'{manifest.upstream}..HEAD'))
    behind = int(_run_git(manifest.worktree, 'rev-list', '--count', f'HEAD..{manifest.upstream}'))
    status = _run_git(manifest.worktree, 'status', '--porcelain=v1', '--untracked-files=normal')
    commits = _commit_identities(manifest.worktree, upstream_sha, original_sha)

    return RepositoryStatus(
        current_branch=current_branch,
        original_sha=original_sha,
        upstream_sha=upstream_sha,
        ahead=ahead,
        behind=behind,
        clean=not status,
        custom_commit_subjects=tuple(commit.subject for commit in commits),
        custom_commits=commits,
    )


def _write_immutable_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{time.time_ns()}.tmp')
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + '\n').encode('utf-8')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o444)
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ManifestError(f'immutable report already exists: {path}') from error
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(encoded).hexdigest()


def _write_candidate_report(result: CandidateResult) -> None:
    payload = {
        'schema': 1,
        'candidate_id': result.candidate_id,
        'status': result.status,
        'state_dir': str(result.state_dir),
        'worktree': str(result.worktree),
        'original_sha': result.original_sha,
        'upstream_sha': result.upstream_sha,
        'candidate_sha': result.candidate_sha,
        'conflicts': list(result.conflicts),
        'original_commit_subjects': list(result.original_commit_subjects),
        'candidate_commit_subjects': list(result.candidate_commit_subjects),
        'original_commits': [
            {'sha': item.sha, 'subject': item.subject, 'patch_id': item.patch_id}
            for item in result.original_commits
        ],
        'candidate_commits': [
            {'sha': item.sha, 'subject': item.subject, 'patch_id': item.patch_id}
            for item in result.candidate_commits
        ],
        'recommendations': [
            {
                'feature_id': item.feature_id,
                'kind': item.kind,
                'commit_subject': item.commit_subject,
            }
            for item in result.recommendations
        ],
    }
    _write_immutable_json(result.state_dir / 'report.json', payload)


def _commit_identity_payload(item: CommitIdentity) -> dict[str, str]:
    return {'sha': item.sha, 'subject': item.subject, 'patch_id': item.patch_id}


def _transformed_commit_payloads(
    original_commits: tuple[CommitIdentity, ...],
    candidate_commits: tuple[CommitIdentity, ...],
) -> list[dict[str, str]]:
    if tuple(item.subject for item in candidate_commits) != tuple(
        item.subject for item in original_commits
    ):
        raise ManifestError('resolved candidate commit subjects do not preserve the original patch stack')

    transformed: list[dict[str, str]] = []
    for original, candidate in zip(original_commits, candidate_commits, strict=True):
        if original.patch_id == candidate.patch_id:
            continue
        transformed.append(
            {
                'original_sha': original.sha,
                'candidate_sha': candidate.sha,
                'subject': original.subject,
                'original_patch_id': original.patch_id,
                'candidate_patch_id': candidate.patch_id,
            }
        )
    return transformed


def prepare_candidate(
    manifest: ManagedUpdateManifest,
    state_root: Path,
    *,
    candidate_id: str,
) -> CandidateResult:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', candidate_id):
        raise ManifestError('candidate_id contains unsupported characters')

    repository = inspect_repository(manifest)
    if repository.current_branch != manifest.branch:
        raise ManifestError(
            f'active branch is {repository.current_branch!r}, expected {manifest.branch!r}'
        )
    if not repository.clean:
        raise ManifestError('active worktree must be clean before preparing an update')

    state_dir = state_root.expanduser().resolve() / candidate_id
    candidate_worktree = state_dir / 'worktree'
    state_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    state_dir.chmod(0o700)
    _run_git(
        manifest.worktree,
        'worktree',
        'add',
        '--detach',
        str(candidate_worktree),
        repository.original_sha,
    )

    rebase = subprocess.run(
        ['git', 'rebase', '--reapply-cherry-picks', repository.upstream_sha],
        cwd=candidate_worktree,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            'GIT_EDITOR': 'true',
            'GIT_SEQUENCE_EDITOR': 'true',
        },
    )
    conflicts_raw = _run_git(
        candidate_worktree,
        'diff',
        '--name-only',
        '--diff-filter=U',
    )
    conflicts = tuple(line for line in conflicts_raw.splitlines() if line)
    if rebase.returncode != 0 and not conflicts:
        detail = rebase.stderr.strip() or rebase.stdout.strip() or f'exit {rebase.returncode}'
        raise ManifestError(f'candidate rebase failed: {detail}')

    candidate_sha = _run_git(candidate_worktree, 'rev-parse', 'HEAD^{commit}')
    _run_git(
        candidate_worktree,
        'merge-base',
        '--is-ancestor',
        repository.upstream_sha,
        candidate_sha,
    )
    candidate_commits = _commit_identities(
        candidate_worktree,
        repository.upstream_sha,
        candidate_sha,
    )
    candidate_subjects = tuple(item.subject for item in candidate_commits)
    recommendations: list[OptimizationRecommendation] = []
    preservation_conflicts: list[str] = []
    if not conflicts:
        remaining_patch_ids: dict[str, int] = {}
        for item in candidate_commits:
            if item.patch_id:
                remaining_patch_ids[item.patch_id] = remaining_patch_ids.get(item.patch_id, 0) + 1
        upstream_equivalents = _upstream_equivalent_commit_shas(
            manifest.worktree,
            repository.upstream_sha,
            repository.original_sha,
        )
        feature_ids_by_subject: dict[str, list[str]] = {}
        for feature in manifest.features:
            feature_ids_by_subject.setdefault(feature.commit_subject, []).append(feature.id)

        for item in repository.custom_commits:
            if item.patch_id and remaining_patch_ids.get(item.patch_id, 0) > 0:
                remaining_patch_ids[item.patch_id] -= 1
                continue
            feature_ids = feature_ids_by_subject.get(item.subject, [])
            feature_id = feature_ids[0] if feature_ids else f'commit-{item.sha[:12]}'
            if item.sha in upstream_equivalents:
                recommendations.append(
                    OptimizationRecommendation(
                        feature_id=feature_id,
                        kind='upstream-equivalent',
                        commit_subject=item.subject,
                    )
                )
                continue
            preservation_conflicts.append(
                f'custom commit {item.sha[:12]} ({item.subject}) is not semantically equivalent '
                'to the candidate or captured upstream'
            )

    all_conflicts = (*conflicts, *preservation_conflicts)
    recommendation_tuple = tuple(recommendations)
    status = 'conflict' if all_conflicts else ('review' if recommendation_tuple else 'ready')
    result = CandidateResult(
        candidate_id=candidate_id,
        state_dir=state_dir,
        worktree=candidate_worktree,
        status=status,
        original_sha=repository.original_sha,
        upstream_sha=repository.upstream_sha,
        candidate_sha=candidate_sha,
        conflicts=all_conflicts,
        original_commit_subjects=repository.custom_commit_subjects,
        candidate_commit_subjects=candidate_subjects,
        recommendations=recommendation_tuple,
        original_commits=repository.custom_commits,
        candidate_commits=candidate_commits,
    )
    _write_candidate_report(result)
    return result


def _candidate_path(root: Path, relative: str, *, must_exist: bool) -> Path:
    value = Path(relative)
    if value.is_absolute():
        raise ManifestError('candidate-relative paths must not be absolute')
    resolved_root = root.resolve()
    resolved = (resolved_root / value).resolve(strict=must_exist)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ManifestError(f'candidate path escapes worktree: {relative}')
    return resolved


def _hash_field(digest: 'hashlib._Hash', value: bytes) -> None:
    digest.update(len(value).to_bytes(8, 'big'))
    digest.update(value)


def _hash_artifact(path: Path) -> str:
    if path.is_file():
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    if not path.is_dir():
        raise ManifestError(f'artifact is neither a file nor a directory: {path}')

    root = path.resolve(strict=True)
    digest = hashlib.sha256()
    digest.update(b'HERMES-ARTIFACT-TREE\0v1\0')
    for child in sorted(path.rglob('*'), key=lambda item: item.relative_to(path).as_posix()):
        relative = child.relative_to(path).as_posix().encode('utf-8')
        stat = child.lstat()
        _hash_field(digest, relative)
        if child.is_symlink():
            target_text = os.readlink(child)
            if Path(target_text).is_absolute():
                raise ManifestError(f'artifact symlink escapes relocatable bundle: {child}')
            try:
                resolved_target = child.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ManifestError(f'artifact symlink is invalid: {child}: {error}') from error
            if resolved_target != root and root not in resolved_target.parents:
                raise ManifestError(f'artifact symlink escapes bundle: {child}')
            digest.update(b'L')
            _hash_field(digest, target_text.encode('utf-8'))
        elif child.is_file():
            digest.update(b'F')
            digest.update((stat.st_mode & 0o777).to_bytes(4, 'big'))
            digest.update(stat.st_size.to_bytes(8, 'big'))
            with child.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
        elif child.is_dir():
            digest.update(b'D')
            digest.update((stat.st_mode & 0o777).to_bytes(4, 'big'))
        else:
            raise ManifestError(f'artifact contains an unsupported file type: {child}')
    return digest.hexdigest()


def _verify_candidate_worktree_identity(
    manifest: ManagedUpdateManifest,
    candidate: CandidateResult,
    phase: str,
) -> None:
    expected_worktree = candidate.worktree.resolve(strict=True)
    top_level = Path(_run_git(candidate.worktree, 'rev-parse', '--show-toplevel')).resolve(strict=True)
    if top_level != expected_worktree:
        raise ManifestError(f'candidate worktree identity changed before {phase}')
    candidate_common = Path(
        _run_git(candidate.worktree, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    ).resolve(strict=True)
    active_common = Path(
        _run_git(manifest.worktree, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    ).resolve(strict=True)
    if candidate_common != active_common:
        raise ManifestError(f'candidate worktree belongs to a different repository before {phase}')


def _verify_candidate_source(
    manifest: ManagedUpdateManifest,
    candidate: CandidateResult,
    phase: str,
) -> None:
    if not re.fullmatch(r'[0-9a-f]{40}', candidate.candidate_sha):
        raise ManifestError('candidate SHA must be a full 40-character hexadecimal SHA')
    _verify_candidate_worktree_identity(manifest, candidate, phase)
    head = _run_git(candidate.worktree, 'rev-parse', 'HEAD^{commit}')
    if head != candidate.candidate_sha:
        raise ManifestError(f'candidate HEAD changed before {phase}')
    tracked_status = _run_git(
        candidate.worktree,
        'status',
        '--porcelain=v1',
        '--untracked-files=no',
        '--ignore-submodules=none',
    )
    if tracked_status:
        raise ManifestError(f'candidate tracked or index state is dirty before {phase}')
    current_candidate_commits = _commit_identities(
        candidate.worktree,
        candidate.upstream_sha,
        candidate.candidate_sha,
    )
    if current_candidate_commits != candidate.candidate_commits:
        raise ManifestError(f'candidate patch identities changed before {phase}')
    current_original_commits = _commit_identities(
        manifest.worktree,
        candidate.upstream_sha,
        candidate.original_sha,
    )
    if current_original_commits != candidate.original_commits:
        raise ManifestError(f'original patch identities changed before {phase}')


def _review_decision_digest(candidate: CandidateResult) -> str | None:
    resolution_path = candidate.state_dir / 'conflict-resolution.json'
    if _path_lexists(resolution_path):
        persisted = load_candidate(candidate.state_dir)
        if persisted != candidate:
            raise ManifestError('conflict resolution does not match the effective candidate')
        resolution = _read_json_object(resolution_path)
        if (
            resolution.get('schema') != 1
            or resolution.get('status') != 'resolved'
            or _required_string(resolution, 'candidate_id') != candidate.candidate_id
            or _report_sha(resolution, 'candidate_sha') != candidate.candidate_sha
            or _report_sha(resolution, 'original_sha') != candidate.original_sha
            or _report_sha(resolution, 'upstream_sha') != candidate.upstream_sha
        ):
            raise ManifestError('conflict resolution does not match candidate provenance')
        encoded = json.dumps(resolution, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    if not candidate.recommendations:
        return None
    decision = _read_json_object(candidate.state_dir / 'decision.json')
    if decision.get('schema') != 1:
        raise ManifestError('unsupported review decision schema')
    if _required_string(decision, 'candidate_id') != candidate.candidate_id:
        raise ManifestError('review decision candidate_id does not match candidate')
    if decision.get('decision') != 'accept-upstream-equivalents':
        raise ManifestError('review decision is not an accepted upstream-equivalent decision')
    expected_features = [item.feature_id for item in candidate.recommendations]
    if decision.get('features') != expected_features:
        raise ManifestError('review decision features do not match candidate recommendations')
    encoded = json.dumps(decision, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def verify_candidate(
    manifest: ManagedUpdateManifest,
    candidate: CandidateResult,
) -> VerifiedCandidate:
    if candidate.status != 'ready':
        raise ManifestError(f'candidate status must be ready, got {candidate.status!r}')
    if not manifest.artifact:
        raise ManifestError('manifest does not configure an artifact')
    _verify_candidate_source(manifest, candidate, 'verification')
    decision_sha256 = _review_decision_digest(candidate)

    command_results: list[VerificationCommandResult] = []
    for command in manifest.verification_commands:
        cwd = _candidate_path(candidate.worktree, command.cwd, must_exist=True)
        if not cwd.is_dir():
            raise ManifestError(f'verification cwd is not a directory: {command.cwd}')
        completed = subprocess.run(
            list(command.argv),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                'CI': '1',
                'GITHUB_SHA': candidate.candidate_sha,
                'GITHUB_REF_NAME': manifest.branch,
            },
        )
        result = VerificationCommandResult(
            name=command.name,
            returncode=completed.returncode,
            stdout=completed.stdout[-20000:],
            stderr=completed.stderr[-20000:],
        )
        command_results.append(result)
        if completed.returncode != 0:
            raise ManifestError(
                f'verification command {command.name!r} failed with exit {completed.returncode}'
            )

    current_decision_sha256 = _review_decision_digest(candidate)
    if current_decision_sha256 != decision_sha256:
        raise ManifestError('review decision changed during candidate verification')
    _verify_candidate_source(manifest, candidate, 'artifact hashing')
    artifact = _candidate_path(candidate.worktree, manifest.artifact, must_exist=True)
    confirmation_token = secrets.token_hex(32)
    verified = VerifiedCandidate(
        candidate_id=candidate.candidate_id,
        status='verified',
        artifact=artifact,
        artifact_sha256=_hash_artifact(artifact),
        manifest_sha256=manifest_digest(manifest),
        decision_sha256=decision_sha256,
        commands=tuple(command_results),
        approval_token_sha256=hashlib.sha256(confirmation_token.encode('ascii')).hexdigest(),
        confirmation_token=confirmation_token,
    )
    report = {
        'schema': 1,
        'candidate_id': verified.candidate_id,
        'candidate_sha': candidate.candidate_sha,
        'status': verified.status,
        'artifact': str(verified.artifact),
        'artifact_sha256': verified.artifact_sha256,
        'manifest_sha256': verified.manifest_sha256,
        'decision_sha256': verified.decision_sha256,
        'approval_token_sha256': verified.approval_token_sha256,
        'commands': [
            {
                'name': item.name,
                'returncode': item.returncode,
                'stdout': item.stdout,
                'stderr': item.stderr,
            }
            for item in verified.commands
        ],
    }
    report_sha256 = _write_immutable_json(candidate.state_dir / 'verification.json', report)
    return replace(verified, verification_report_sha256=report_sha256)


def accept_candidate_review(
    manifest: ManagedUpdateManifest,
    candidate: CandidateResult,
) -> VerifiedCandidate:
    if candidate.status != 'review':
        raise ManifestError(f'candidate status must be review, got {candidate.status!r}')
    if not candidate.recommendations:
        raise ManifestError('review candidate has no optimization recommendations')

    decision = {
        'schema': 1,
        'candidate_id': candidate.candidate_id,
        'decision': 'accept-upstream-equivalents',
        'features': [item.feature_id for item in candidate.recommendations],
        'accepted_at': int(time.time()),
    }
    _write_immutable_json(candidate.state_dir / 'decision.json', decision)

    ready = replace(candidate, status='ready')
    return verify_candidate(manifest, ready)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f'cannot read report {path}: {error}') from error
    if not isinstance(value, dict):
        raise ManifestError(f'report must contain a JSON object: {path}')
    return value


def _report_string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ManifestError(f'report field {field!r} must be a string array')
    return tuple(value)


def _report_sha(report: dict[str, Any], field: str) -> str:
    value = _required_string(report, field)
    if not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ManifestError(f'report field {field!r} must be a full 40-character SHA')
    return value


def _report_commit_identities(value: Any, field: str) -> tuple[CommitIdentity, ...]:
    if not isinstance(value, list):
        raise ManifestError(f'report field {field!r} must be a commit identity array')
    identities: list[CommitIdentity] = []
    for row in value:
        if not isinstance(row, dict):
            raise ManifestError(f'report field {field!r} contains a non-object commit identity')
        sha = _required_string(row, 'sha')
        subject = _required_string(row, 'subject')
        patch_id = row.get('patch_id')
        if not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ManifestError(f'report field {field!r} contains an invalid commit SHA')
        if not isinstance(patch_id, str) or (
            patch_id and not re.fullmatch(r'[0-9a-f]{40,64}', patch_id)
        ):
            raise ManifestError(f'report field {field!r} contains an invalid patch-id')
        identities.append(CommitIdentity(sha=sha, subject=subject, patch_id=patch_id))
    return tuple(identities)


def load_candidate(state_dir: Path, *, allow_cancelled: bool = False) -> CandidateResult:
    state_dir = Path(state_dir)
    if not allow_cancelled:
        for marker_name in ('cancellation-intent.json', 'cancellation.json'):
            marker_path = state_dir / marker_name
            if _path_lexists(marker_path):
                marker = _read_json_object(marker_path)
                expected_status = 'cancelling' if marker_name == 'cancellation-intent.json' else 'cancelled'
                if (
                    marker.get('schema') != 1
                    or marker.get('status') != expected_status
                    or marker.get('candidate_id') != state_dir.resolve().name
                ):
                    raise ManifestError(f'candidate has an invalid {marker_name} marker')
                raise ManifestError(f'candidate {state_dir.resolve().name!r} is cancelled')
    report = _read_json_object(state_dir / 'report.json')
    if report.get('schema') != 1:
        raise ManifestError('unsupported candidate report schema')
    candidate_id = _required_string(report, 'candidate_id')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', candidate_id):
        raise ManifestError('candidate report contains an invalid candidate_id')
    if state_dir.resolve().name != candidate_id:
        raise ManifestError('candidate report candidate_id does not match its directory')
    reported_state_dir = Path(_required_string(report, 'state_dir'))
    if reported_state_dir.resolve() != state_dir.resolve():
        raise ManifestError('candidate report state_dir does not match its location')
    worktree = Path(_required_string(report, 'worktree'))
    if worktree.resolve() != (state_dir / 'worktree').resolve():
        raise ManifestError('candidate worktree does not match its state directory')
    status = _required_string(report, 'status')
    if status not in {'conflict', 'review', 'ready'}:
        raise ManifestError('candidate report contains an invalid status')

    recommendations_value = report.get('recommendations', [])
    if not isinstance(recommendations_value, list):
        raise ManifestError('candidate recommendations must be an array')
    recommendations: list[OptimizationRecommendation] = []
    for item in recommendations_value:
        if not isinstance(item, dict):
            raise ManifestError('candidate recommendation must be an object')
        feature_id = _required_string(item, 'feature_id')
        kind = _required_string(item, 'kind')
        if kind != 'upstream-equivalent':
            raise ManifestError('candidate recommendation contains an invalid kind')
        recommendations.append(
            OptimizationRecommendation(
                feature_id=feature_id,
                kind=kind,
                commit_subject=_required_string(item, 'commit_subject'),
            )
        )

    conflicts = _report_string_list(report.get('conflicts', []), 'conflicts')
    if status == 'conflict' and not conflicts:
        raise ManifestError('conflict candidate report has no conflicts')
    if status == 'review' and not recommendations:
        raise ManifestError('review candidate report has no recommendations')
    if status == 'ready' and (conflicts or recommendations):
        raise ManifestError('ready candidate report contains unresolved decisions')

    original_subjects = _report_string_list(
        report.get('original_commit_subjects', []),
        'original_commit_subjects',
    )
    candidate_subjects = _report_string_list(
        report.get('candidate_commit_subjects', []),
        'candidate_commit_subjects',
    )
    original_commits = _report_commit_identities(report.get('original_commits'), 'original_commits')
    candidate_commits = _report_commit_identities(report.get('candidate_commits'), 'candidate_commits')
    if original_subjects != tuple(item.subject for item in original_commits):
        raise ManifestError('original commit subjects do not match commit identities')
    if candidate_subjects != tuple(item.subject for item in candidate_commits):
        raise ManifestError('candidate commit subjects do not match commit identities')

    original_sha = _report_sha(report, 'original_sha')
    upstream_sha = _report_sha(report, 'upstream_sha')
    candidate_sha = _report_sha(report, 'candidate_sha')

    resolution_path = state_dir / 'conflict-resolution.json'
    if _path_lexists(resolution_path):
        if status != 'conflict':
            raise ManifestError('only a conflict candidate may contain a conflict resolution')
        resolution = _read_json_object(resolution_path)
        if resolution.get('schema') != 1 or resolution.get('status') != 'resolved':
            raise ManifestError('candidate conflict resolution schema or status is invalid')
        if _required_string(resolution, 'candidate_id') != candidate_id:
            raise ManifestError('candidate conflict resolution candidate_id does not match')
        if Path(_required_string(resolution, 'state_dir')).resolve() != state_dir.resolve():
            raise ManifestError('candidate conflict resolution state_dir does not match')
        if Path(_required_string(resolution, 'worktree')).resolve() != worktree.resolve():
            raise ManifestError('candidate conflict resolution worktree does not match')
        if _report_sha(resolution, 'original_sha') != original_sha:
            raise ManifestError('candidate conflict resolution original SHA does not match')
        if _report_sha(resolution, 'upstream_sha') != upstream_sha:
            raise ManifestError('candidate conflict resolution upstream SHA does not match')
        if _report_sha(resolution, 'prior_candidate_sha') != candidate_sha:
            raise ManifestError('candidate conflict resolution prior SHA does not match')
        if resolution.get('conflicts') != list(conflicts):
            raise ManifestError('candidate conflict resolution conflicts do not match')

        resolved_subjects = _report_string_list(
            resolution.get('candidate_commit_subjects', []),
            'candidate_commit_subjects',
        )
        resolved_commits = _report_commit_identities(
            resolution.get('candidate_commits'),
            'candidate_commits',
        )
        if resolved_subjects != tuple(item.subject for item in resolved_commits):
            raise ManifestError('resolved commit subjects do not match commit identities')
        expected_transformed = _transformed_commit_payloads(original_commits, resolved_commits)
        if resolution.get('transformed_commits') != expected_transformed:
            raise ManifestError('candidate conflict resolution transformed commits do not match')

        candidate_sha = _report_sha(resolution, 'candidate_sha')
        candidate_subjects = resolved_subjects
        candidate_commits = resolved_commits
        status = 'ready'
        conflicts = ()
        recommendations = []

    if _path_lexists(worktree):
        if _commit_identities(worktree, upstream_sha, original_sha) != original_commits:
            raise ManifestError('original patch identities do not match the candidate repository')
        if _commit_identities(worktree, upstream_sha, candidate_sha) != candidate_commits:
            raise ManifestError('candidate patch identities do not match the candidate repository')
    elif not allow_cancelled:
        raise ManifestError('candidate worktree is missing')

    return CandidateResult(
        candidate_id=candidate_id,
        state_dir=reported_state_dir,
        worktree=worktree,
        status=status,
        original_sha=original_sha,
        upstream_sha=upstream_sha,
        candidate_sha=candidate_sha,
        conflicts=conflicts,
        original_commit_subjects=original_subjects,
        candidate_commit_subjects=candidate_subjects,
        recommendations=tuple(recommendations),
        original_commits=original_commits,
        candidate_commits=candidate_commits,
    )


def resume_candidate_conflict(
    manifest: ManagedUpdateManifest,
    candidate: CandidateResult,
) -> CandidateResult:
    if candidate.status != 'conflict':
        raise ManifestError(f'candidate status must be conflict, got {candidate.status!r}')

    persisted = load_candidate(candidate.state_dir)
    if persisted != candidate:
        raise ManifestError('candidate conflict resolution request does not match its persisted report')
    _verify_candidate_worktree_identity(manifest, candidate, 'conflict resolution')

    for rebase_name in ('rebase-merge', 'rebase-apply'):
        rebase_path = Path(
            _run_git(
                candidate.worktree,
                'rev-parse',
                '--path-format=absolute',
                '--git-path',
                rebase_name,
            )
        )
        if _path_lexists(rebase_path):
            raise ManifestError('candidate rebase is still in progress; resolve and continue it first')

    tracked_status = _run_git(
        candidate.worktree,
        'status',
        '--porcelain=v1',
        '--untracked-files=no',
        '--ignore-submodules=none',
    )
    if tracked_status:
        raise ManifestError('resolved candidate tracked or index state is dirty')

    candidate_sha = _run_git(candidate.worktree, 'rev-parse', 'HEAD^{commit}')
    _run_git(
        candidate.worktree,
        'merge-base',
        '--is-ancestor',
        candidate.upstream_sha,
        candidate_sha,
    )
    candidate_commits = _commit_identities(
        candidate.worktree,
        candidate.upstream_sha,
        candidate_sha,
    )
    candidate_subjects = tuple(item.subject for item in candidate_commits)
    transformed_commits = _transformed_commit_payloads(
        candidate.original_commits,
        candidate_commits,
    )
    resolution = {
        'schema': 1,
        'status': 'resolved',
        'candidate_id': candidate.candidate_id,
        'state_dir': str(candidate.state_dir),
        'worktree': str(candidate.worktree),
        'original_sha': candidate.original_sha,
        'upstream_sha': candidate.upstream_sha,
        'prior_candidate_sha': candidate.candidate_sha,
        'candidate_sha': candidate_sha,
        'conflicts': list(candidate.conflicts),
        'candidate_commit_subjects': list(candidate_subjects),
        'candidate_commits': [_commit_identity_payload(item) for item in candidate_commits],
        'transformed_commits': transformed_commits,
        'resolved_at': int(time.time()),
    }
    _write_immutable_json(candidate.state_dir / 'conflict-resolution.json', resolution)
    return load_candidate(candidate.state_dir)


def invalidate_candidate_state(state_dir: Path, candidate_id: str) -> Path:
    state_dir = Path(state_dir)
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise ManifestError('candidate state directory must be a real directory')
    if state_dir.resolve().name != candidate_id:
        raise ManifestError('candidate state directory does not match candidate_id')

    intent_path = state_dir / 'cancellation-intent.json'
    if _path_lexists(intent_path):
        intent = _read_json_object(intent_path)
        if (
            intent.get('schema') != 1
            or intent.get('status') != 'cancelling'
            or intent.get('candidate_id') != candidate_id
        ):
            raise ManifestError('candidate has an invalid cancellation intent')
        return intent_path

    _write_immutable_json(
        intent_path,
        {
            'schema': 1,
            'status': 'cancelling',
            'candidate_id': candidate_id,
            'requested_at': int(time.time()),
        },
    )
    return intent_path


def cancel_candidate(
    manifest: ManagedUpdateManifest,
    candidate: CandidateResult,
) -> Path:
    final_path = candidate.state_dir / 'cancellation.json'
    if _path_lexists(final_path):
        final = _read_json_object(final_path)
        if (
            final.get('schema') != 1
            or final.get('status') != 'cancelled'
            or final.get('candidate_id') != candidate.candidate_id
        ):
            raise ManifestError('candidate has an invalid cancellation report')
        return final_path

    persisted = load_candidate(candidate.state_dir, allow_cancelled=True)
    if persisted != candidate:
        raise ManifestError('candidate cancellation request does not match its persisted report')

    intent_path = candidate.state_dir / 'cancellation-intent.json'
    intent_exists = _path_lexists(intent_path)
    if intent_exists:
        intent = _read_json_object(intent_path)
        if (
            intent.get('schema') != 1
            or intent.get('status') != 'cancelling'
            or intent.get('candidate_id') != candidate.candidate_id
        ):
            raise ManifestError('candidate has an invalid cancellation intent')
    else:
        _verify_candidate_worktree_identity(manifest, candidate, 'cancellation')
        _write_immutable_json(
            intent_path,
            {
                'schema': 1,
                'status': 'cancelling',
                'candidate_id': candidate.candidate_id,
                'candidate_sha': candidate.candidate_sha,
                'manifest_sha256': manifest_digest(manifest),
                'requested_at': int(time.time()),
            },
        )

    if _path_lexists(candidate.worktree):
        _verify_candidate_worktree_identity(manifest, candidate, 'cancellation cleanup')
        removed = subprocess.run(
            ['git', 'worktree', 'remove', '--force', str(candidate.worktree)],
            cwd=manifest.worktree,
            check=False,
            capture_output=True,
            text=True,
        )
        if removed.returncode != 0:
            detail = removed.stderr.strip() or removed.stdout.strip()
            raise ManifestError(f'cannot remove cancelled candidate worktree: {detail}')
    if _path_lexists(candidate.worktree):
        raise ManifestError('cancelled candidate worktree still exists after cleanup')

    _write_immutable_json(
        final_path,
        {
            'schema': 1,
            'status': 'cancelled',
            'candidate_id': candidate.candidate_id,
            'candidate_sha': candidate.candidate_sha,
            'manifest_sha256': manifest_digest(manifest),
            'cancelled_at': int(time.time()),
        },
    )
    return final_path


def load_verified_candidate(
    state_dir: Path,
    candidate: CandidateResult,
) -> VerifiedCandidate:
    report_path = Path(state_dir) / 'verification.json'
    report = _read_json_object(report_path)
    if report.get('schema') != 1:
        raise ManifestError('unsupported verification report schema')
    if _required_string(report, 'candidate_id') != candidate.candidate_id:
        raise ManifestError('verification report candidate_id does not match candidate')
    if _required_string(report, 'candidate_sha') != candidate.candidate_sha:
        raise ManifestError('verification report candidate SHA does not match candidate')
    if _required_string(report, 'status') != 'verified':
        raise ManifestError('verification report status must be verified')

    command_rows = report.get('commands', [])
    if not isinstance(command_rows, list):
        raise ManifestError('verification report commands must be an array')
    commands: list[VerificationCommandResult] = []
    for row in command_rows:
        if not isinstance(row, dict):
            raise ManifestError('verification command result must be an object')
        returncode = row.get('returncode')
        if not isinstance(returncode, int):
            raise ManifestError('verification command returncode must be an integer')
        if returncode != 0:
            raise ManifestError('verification report contains a failed command')
        commands.append(
            VerificationCommandResult(
                name=_required_string(row, 'name'),
                returncode=returncode,
                stdout=str(row.get('stdout', '')),
                stderr=str(row.get('stderr', '')),
            )
        )

    artifact_value = _required_string(report, 'artifact')
    artifact = Path(artifact_value).resolve()
    candidate_root = candidate.worktree.resolve()
    if artifact != candidate_root and candidate_root not in artifact.parents:
        raise ManifestError('verified artifact is outside the candidate worktree')
    artifact_sha256 = _required_string(report, 'artifact_sha256')
    if not re.fullmatch(r'[0-9a-f]{64}', artifact_sha256):
        raise ManifestError('verification report contains an invalid artifact hash')
    manifest_sha256 = _required_string(report, 'manifest_sha256')
    if not re.fullmatch(r'[0-9a-f]{64}', manifest_sha256):
        raise ManifestError('verification report contains an invalid manifest hash')
    decision_value = report.get('decision_sha256')
    if decision_value is not None and (
        not isinstance(decision_value, str) or not re.fullmatch(r'[0-9a-f]{64}', decision_value)
    ):
        raise ManifestError('verification report contains an invalid decision hash')
    current_decision_sha256 = _review_decision_digest(candidate)
    if decision_value != current_decision_sha256:
        raise ManifestError('verification report decision hash does not match current decision')
    approval_token_sha256 = _required_string(report, 'approval_token_sha256')
    if not re.fullmatch(r'[0-9a-f]{64}', approval_token_sha256):
        raise ManifestError('verification report contains an invalid approval token hash')
    return VerifiedCandidate(
        candidate_id=candidate.candidate_id,
        status='verified',
        artifact=artifact,
        artifact_sha256=artifact_sha256,
        manifest_sha256=manifest_sha256,
        decision_sha256=current_decision_sha256,
        commands=tuple(commands),
        approval_token_sha256=approval_token_sha256,
        verification_report_sha256=_hash_artifact(report_path),
    )


def approval_token(
    manifest: ManagedUpdateManifest,
    candidate: CandidateResult,
    verified: VerifiedCandidate,
) -> str:
    del manifest, candidate
    if not re.fullmatch(r'[0-9a-f]{64}', verified.confirmation_token):
        raise ManifestError('raw approval capability is not available')
    digest = hashlib.sha256(verified.confirmation_token.encode('ascii')).hexdigest()
    if not hmac.compare_digest(digest, verified.approval_token_sha256):
        raise ManifestError('raw approval capability does not match verification report')
    return verified.confirmation_token


def _default_copy_bundle(source: Path, target: Path) -> None:
    ditto = Path('/usr/bin/ditto')
    if ditto.exists():
        completed = subprocess.run(
            [str(ditto), str(source), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ManifestError(f'ditto failed: {detail}')
        return
    shutil.copytree(source, target, symlinks=True)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _rename_swap(first: Path, second: Path) -> None:
    if os.uname().sysname != 'Darwin':
        raise ManifestError('atomic app exchange requires macOS renamex_np support')
    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = libc.renamex_np
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    rename_swap = 0x00000002
    if renamex_np(os.fsencode(first), os.fsencode(second), rename_swap) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), f'{first} <-> {second}')


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _move_bundle(source: Path, destination: Path) -> None:
    source.replace(destination)


def _swap_app_bundle(
    target: Path,
    staged: Path,
    backup: Path,
    health_check: Callable[[Path], bool],
    *,
    move_to_backup: Callable[[Path, Path], None] | None = None,
) -> None:
    mover = move_to_backup or _move_bundle
    exchanged = False
    try:
        _rename_swap(target, staged)
        exchanged = True
        mover(staged, backup)
        if not health_check(target):
            raise ManifestError('installed candidate failed the health check')
    except Exception as original_error:
        rollback_error: Exception | None = None
        if exchanged:
            try:
                staged_exists = _path_lexists(staged)
                backup_exists = _path_lexists(backup)
                if staged_exists == backup_exists:
                    raise ManifestError(
                        'cannot identify exactly one retained old bundle for atomic rollback'
                    )
                rollback_source = staged if staged_exists else backup
                _rename_swap(target, rollback_source)
                _remove_path(rollback_source)
            except Exception as error:
                rollback_error = error
        else:
            _remove_path(staged)
        if rollback_error is not None:
            raise ManifestError(
                f'app exchange failed ({original_error}); atomic rollback also failed ({rollback_error})'
            ) from original_error
        raise


@contextmanager
def _repository_update_lock(manifest: ManagedUpdateManifest):
    common_dir = Path(
        _run_git(
            manifest.worktree,
            'rev-parse',
            '--path-format=absolute',
            '--git-common-dir',
        )
    ).resolve()
    lock_path = common_dir / 'hermes-managed-update.lock'
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _transaction_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ManifestError(f'install transaction field {field!r} must be a path')
    return Path(value).expanduser().resolve()


@dataclass(frozen=True)
class _InstallTransactionMetadata:
    candidate_id: str
    manifest_sha256: str
    original_sha: str
    candidate_sha: str
    target: Path
    staged: Path
    backup: Path
    original_artifact_sha256: str
    candidate_artifact_sha256: str
    started_at: int


def _validate_install_transaction_metadata(
    state_dir: Path,
    transaction: dict[str, Any],
    *,
    context: str = 'install transaction',
) -> _InstallTransactionMetadata:
    state_dir = Path(state_dir).resolve()
    if transaction.get('schema') != 1 or transaction.get('status') != 'installing':
        raise ManifestError(f'invalid {context} journal')
    candidate_id = _required_string(transaction, 'candidate_id')
    if candidate_id != state_dir.name or not re.fullmatch(
        r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', candidate_id
    ):
        raise ManifestError(f'{context} candidate identity is invalid')
    manifest_sha256 = _required_string(transaction, 'manifest_sha256')
    if not re.fullmatch(r'[0-9a-f]{64}', manifest_sha256):
        raise ManifestError(f'{context} manifest hash is invalid')

    started_at = transaction.get('started_at')
    if (
        not isinstance(started_at, int)
        or isinstance(started_at, bool)
        or started_at < 0
    ):
        raise ManifestError(f'{context} started_at is invalid')

    original_sha = _report_sha(transaction, 'original_sha')
    candidate_sha = _report_sha(transaction, 'candidate_sha')
    original_artifact_sha256 = _required_string(transaction, 'original_artifact_sha256')
    candidate_artifact_sha256 = _required_string(transaction, 'candidate_artifact_sha256')
    if (
        not re.fullmatch(r'[0-9a-f]{64}', original_artifact_sha256)
        or not re.fullmatch(r'[0-9a-f]{64}', candidate_artifact_sha256)
        or original_artifact_sha256 == candidate_artifact_sha256
    ):
        raise ManifestError(f'{context} contains invalid bundle hashes')

    target = _transaction_path(transaction.get('target'), 'target')
    staged = _transaction_path(transaction.get('staged'), 'staged')
    backup = _transaction_path(transaction.get('backup'), 'backup')
    if (
        staged != target.with_name(f'.{target.stem}.install-{candidate_id}.app')
        or backup != target.with_name(f'.{target.stem}.rollback-{candidate_id}.app')
    ):
        raise ManifestError(f'{context} bundle paths are invalid')

    return _InstallTransactionMetadata(
        candidate_id=candidate_id,
        manifest_sha256=manifest_sha256,
        original_sha=original_sha,
        candidate_sha=candidate_sha,
        target=target,
        staged=staged,
        backup=backup,
        original_artifact_sha256=original_artifact_sha256,
        candidate_artifact_sha256=candidate_artifact_sha256,
        started_at=started_at,
    )


def _validate_install_terminal_metadata(
    state_dir: Path,
    metadata: _InstallTransactionMetadata,
    *,
    allow_pending: bool,
    historical: bool = False,
) -> tuple[Path, str] | None:
    install_report = state_dir / 'install.json'
    recovery_report = state_dir / 'recovery.json'
    terminal_reports = [
        report for report in (install_report, recovery_report) if _path_lexists(report)
    ]
    if not terminal_reports and allow_pending:
        return None
    if len(terminal_reports) != 1:
        prefix = 'historical ' if historical else ''
        raise ManifestError(f'{prefix}install transaction is not uniquely terminal')

    report_path = terminal_reports[0]
    terminal = _read_json_object(report_path)
    if report_path == install_report:
        valid = (
            terminal.get('schema') == 1
            and terminal.get('candidate_id') == metadata.candidate_id
            and terminal.get('status') == 'installed'
            and terminal.get('original_sha') == metadata.original_sha
            and terminal.get('candidate_sha') == metadata.candidate_sha
            and terminal.get('artifact_sha256') == metadata.candidate_artifact_sha256
            and terminal.get('safety_ref')
            == f'refs/hermes-managed-update/safety/{metadata.candidate_id}'
        )
        kind = 'installed'
    else:
        recovered_at = terminal.get('recovered_at')
        valid = (
            terminal.get('schema') == 1
            and terminal.get('candidate_id') == metadata.candidate_id
            and terminal.get('status') == 'rolled-back'
            and terminal.get('original_sha') == metadata.original_sha
            and terminal.get('candidate_sha') == metadata.candidate_sha
            and terminal.get('original_artifact_sha256')
            == metadata.original_artifact_sha256
            and terminal.get('candidate_artifact_sha256')
            == metadata.candidate_artifact_sha256
            and isinstance(recovered_at, int)
            and not isinstance(recovered_at, bool)
            and recovered_at >= 0
        )
        kind = 'rolled-back'
    if not valid:
        if historical:
            raise ManifestError('invalid historical install transaction report')
        if kind == 'installed':
            raise ManifestError('invalid committed install report')
        raise ManifestError('invalid install recovery report')
    return report_path, kind


def _next_install_started_at(state_root: Path) -> int:
    state_root = Path(state_root).expanduser().resolve()
    if not state_root.is_dir() or state_root.is_symlink():
        raise ManifestError('managed update state root must be a real directory')

    existing: set[int] = set()
    for state_dir in sorted(state_root.iterdir(), key=lambda item: item.name):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', state_dir.name):
            continue
        if not state_dir.is_dir() or state_dir.is_symlink():
            continue
        transaction_path = state_dir / 'transaction.json'
        if not _path_lexists(transaction_path):
            continue
        transaction = _read_json_object(transaction_path)
        started_at = transaction.get('started_at')
        if (
            not isinstance(started_at, int)
            or isinstance(started_at, bool)
            or started_at < 0
        ):
            raise ManifestError('existing install transaction started_at is invalid')
        if started_at in existing:
            raise ManifestError('existing install transactions have ambiguous started_at ordering')
        existing.add(started_at)

    minimum = max(existing, default=-1) + 1
    return max(time.time_ns(), minimum)


def _recover_install_transaction_locked(
    manifest: ManagedUpdateManifest,
    state_dir: Path,
) -> Path:
    state_dir = Path(state_dir).resolve()
    transaction = _read_json_object(state_dir / 'transaction.json')
    metadata = _validate_install_transaction_metadata(state_dir, transaction)
    if metadata.manifest_sha256 != manifest_digest(manifest):
        raise ManifestError('install transaction manifest does not match current configuration')

    expected_target = manifest.installed_app.resolve()
    if metadata.target != expected_target:
        raise ManifestError('install transaction bundle paths do not match the manifest and candidate')
    if _run_git(manifest.worktree, 'branch', '--show-current') != manifest.branch:
        raise ManifestError('active branch does not match the managed update manifest')

    terminal = _validate_install_terminal_metadata(
        state_dir,
        metadata,
        allow_pending=True,
    )
    head = _run_git(manifest.worktree, 'rev-parse', 'HEAD^{commit}')
    if terminal is not None:
        report_path, kind = terminal
        if kind == 'installed':
            if head != metadata.candidate_sha:
                raise ManifestError('committed install report does not match active Git HEAD')
            if (
                not _path_lexists(metadata.target)
                or _hash_artifact(metadata.target) != metadata.candidate_artifact_sha256
            ):
                raise ManifestError(
                    'committed install report does not match the installed app bundle'
                )
            if (
                not _path_lexists(metadata.backup)
                or _hash_artifact(metadata.backup) != metadata.original_artifact_sha256
            ):
                raise ManifestError(
                    'committed install report does not match the retained rollback bundle'
                )
            if _path_lexists(metadata.staged):
                raise ManifestError('committed install left an unexpected staging bundle')
            safety_ref = f'refs/hermes-managed-update/safety/{metadata.candidate_id}'
            try:
                safety_sha = _run_git(
                    manifest.worktree,
                    'rev-parse',
                    f'{safety_ref}^{{commit}}',
                )
            except ManifestError as error:
                raise ManifestError('committed install safety ref is missing or invalid') from error
            if safety_sha != metadata.original_sha:
                raise ManifestError('committed install safety ref does not match original Git HEAD')
            return report_path

        if head != metadata.original_sha:
            raise ManifestError('install recovery report does not match active Git HEAD')
        if (
            not _path_lexists(metadata.target)
            or _hash_artifact(metadata.target) != metadata.original_artifact_sha256
        ):
            raise ManifestError('install recovery report does not match the installed app bundle')
        if _path_lexists(metadata.staged) or _path_lexists(metadata.backup):
            raise ManifestError('install recovery report left an unexpected side bundle')
        return report_path

    if not _path_lexists(metadata.target):
        raise ManifestError('cannot recover install transaction because the target app is missing')
    if head not in (metadata.original_sha, metadata.candidate_sha):
        raise ManifestError('active Git HEAD matches neither side of the install transaction')
    if head == metadata.candidate_sha and _run_git(
        manifest.worktree, 'status', '--porcelain'
    ):
        raise ManifestError('active worktree is dirty and cannot be rolled back safely')

    def bundle_hash(path: Path) -> str | None:
        return _hash_artifact(path) if _path_lexists(path) else None

    target_hash = bundle_hash(metadata.target)
    side_hashes = {
        path: bundle_hash(path) for path in (metadata.staged, metadata.backup)
    }
    for path, digest in side_hashes.items():
        if digest is not None and digest not in (
            metadata.original_artifact_sha256,
            metadata.candidate_artifact_sha256,
        ):
            raise ManifestError(f'cannot safely remove unrecognized recovery bundle: {path}')

    original_source: Path | None = None
    if target_hash == metadata.candidate_artifact_sha256:
        old_sources = [
            path
            for path, digest in side_hashes.items()
            if digest == metadata.original_artifact_sha256
        ]
        if len(old_sources) != 1:
            raise ManifestError(
                'cannot identify exactly one original bundle for transaction recovery'
            )
        original_source = old_sources[0]
    elif target_hash != metadata.original_artifact_sha256:
        raise ManifestError('target app hash matches neither the original nor candidate bundle')

    # Every fail-closed precondition above is complete before the first mutation.
    if head == metadata.candidate_sha:
        _run_git(manifest.worktree, 'reset', '--keep', metadata.original_sha)
    if original_source is not None:
        _rename_swap(metadata.target, original_source)
        side_hashes[original_source] = metadata.candidate_artifact_sha256
    for path, digest in side_hashes.items():
        if digest is not None:
            _remove_path(path)

    if _hash_artifact(metadata.target) != metadata.original_artifact_sha256:
        raise ManifestError('transaction recovery did not restore the original app bundle')
    if _run_git(manifest.worktree, 'rev-parse', 'HEAD^{commit}') != metadata.original_sha:
        raise ManifestError('transaction recovery did not restore the original Git HEAD')

    recovery_report = state_dir / 'recovery.json'
    _write_immutable_json(
        recovery_report,
        {
            'schema': 1,
            'status': 'rolled-back',
            'candidate_id': metadata.candidate_id,
            'original_sha': metadata.original_sha,
            'candidate_sha': metadata.candidate_sha,
            'original_artifact_sha256': metadata.original_artifact_sha256,
            'candidate_artifact_sha256': metadata.candidate_artifact_sha256,
            'recovered_at': int(time.time()),
        },
    )
    return recovery_report


def _recover_pending_install_transactions_locked(
    manifest: ManagedUpdateManifest,
    state_root: Path,
    *,
    required_latest_state_dir: Path | None = None,
) -> tuple[Path, ...]:
    state_root = Path(state_root).expanduser().resolve()
    required_latest = (
        Path(required_latest_state_dir).expanduser().resolve()
        if required_latest_state_dir is not None
        else None
    )
    if not _path_lexists(state_root):
        if required_latest is not None:
            raise ManifestError('requested install transaction is not the latest install transaction')
        return ()
    if not state_root.is_dir() or state_root.is_symlink():
        raise ManifestError('managed update state root must be a real directory')

    transactions: list[tuple[int, Path, _InstallTransactionMetadata]] = []
    for state_dir in sorted(state_root.iterdir(), key=lambda item: item.name):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', state_dir.name):
            continue
        if not state_dir.is_dir() or state_dir.is_symlink():
            continue
        transaction_path = state_dir / 'transaction.json'
        if not _path_lexists(transaction_path):
            continue
        transaction = _read_json_object(transaction_path)
        metadata = _validate_install_transaction_metadata(state_dir, transaction)
        transactions.append((metadata.started_at, state_dir.resolve(), metadata))

    if not transactions:
        if required_latest is not None:
            raise ManifestError('requested install transaction is not the latest install transaction')
        return ()
    started_values = [started_at for started_at, _state_dir, _metadata in transactions]
    if len(set(started_values)) != len(started_values):
        raise ManifestError('multiple install transactions have ambiguous started_at ordering')
    transactions.sort(key=lambda item: item[0])

    for _started_at, historical_state_dir, metadata in transactions[:-1]:
        _validate_install_terminal_metadata(
            historical_state_dir,
            metadata,
            allow_pending=False,
            historical=True,
        )

    current_state_dir = transactions[-1][1]
    if required_latest is not None and current_state_dir != required_latest:
        raise ManifestError('requested install transaction is not the latest install transaction')
    current_metadata = transactions[-1][2]
    terminal = _validate_install_terminal_metadata(
        current_state_dir,
        current_metadata,
        allow_pending=True,
    )
    if terminal is not None and required_latest is None:
        return ()
    report = _recover_install_transaction_locked(manifest, current_state_dir)
    if terminal is not None:
        return ()
    return (report,)


def recover_install_transaction(
    manifest: ManagedUpdateManifest,
    state_dir: Path,
) -> Path:
    state_dir = Path(state_dir).expanduser().resolve()
    with _repository_update_lock(manifest):
        reports = _recover_pending_install_transactions_locked(
            manifest,
            state_dir.parent,
            required_latest_state_dir=state_dir,
        )
        if reports:
            return reports[0]
        install_report = state_dir / 'install.json'
        recovery_report = state_dir / 'recovery.json'
        if _path_lexists(install_report) == _path_lexists(recovery_report):
            raise ManifestError('install transaction is not uniquely terminal')
        return install_report if _path_lexists(install_report) else recovery_report


def recover_pending_install_transactions(
    manifest: ManagedUpdateManifest,
    state_root: Path,
) -> tuple[Path, ...]:
    with _repository_update_lock(manifest):
        return _recover_pending_install_transactions_locked(manifest, state_root)


def validate_install_approval(
    manifest: ManagedUpdateManifest,
    candidate: CandidateResult,
    verified: VerifiedCandidate,
    *,
    confirmation_token: str,
    confirmed_candidate_sha: str,
    confirmed_artifact_sha256: str,
) -> None:
    if not re.fullmatch(r'[0-9a-f]{64}', confirmation_token):
        raise ManifestError('confirmation capability has an invalid format')
    supplied_hash = hashlib.sha256(confirmation_token.encode('ascii')).hexdigest()
    if not hmac.compare_digest(supplied_hash, verified.approval_token_sha256):
        raise ManifestError('confirmation capability does not match the verified candidate')
    if not hmac.compare_digest(confirmed_candidate_sha, candidate.candidate_sha):
        raise ManifestError('confirmed candidate SHA does not match the verified candidate')
    if not hmac.compare_digest(confirmed_artifact_sha256, verified.artifact_sha256):
        raise ManifestError('confirmed artifact hash does not match the verified candidate')
    current_manifest_sha256 = manifest_digest(manifest)
    if verified.manifest_sha256 != current_manifest_sha256:
        raise ManifestError('managed update manifest changed after candidate verification')
    if verified.decision_sha256 != _review_decision_digest(candidate):
        raise ManifestError('review decision changed after candidate verification')
    if verified.candidate_id != candidate.candidate_id or verified.status != 'verified':
        raise ManifestError('verified candidate identity or status is invalid')
    if not manifest.artifact:
        raise ManifestError('manifest does not configure an artifact')
    expected_artifact = _candidate_path(candidate.worktree, manifest.artifact, must_exist=True)
    if verified.artifact.resolve(strict=True) != expected_artifact:
        raise ManifestError('verified artifact does not match the manifest artifact path')


def _install_verified_candidate_locked(
    manifest: ManagedUpdateManifest,
    candidate: CandidateResult,
    verified: VerifiedCandidate,
    *,
    confirmation_token: str,
    confirmed_candidate_sha: str,
    confirmed_artifact_sha256: str,
    copy_bundle: Callable[[Path, Path], None] = _default_copy_bundle,
    health_check: Callable[[Path], bool],
) -> InstallResult:
    validate_install_approval(
        manifest,
        candidate,
        verified,
        confirmation_token=confirmation_token,
        confirmed_candidate_sha=confirmed_candidate_sha,
        confirmed_artifact_sha256=confirmed_artifact_sha256,
    )
    current_manifest_sha256 = manifest_digest(manifest)
    _recover_pending_install_transactions_locked(manifest, candidate.state_dir.parent)
    _verify_candidate_source(manifest, candidate, 'installation')
    if _hash_artifact(verified.artifact) != verified.artifact_sha256:
        raise ManifestError('verified artifact changed after verification')

    repository = inspect_repository(manifest)
    if repository.current_branch != manifest.branch:
        raise ManifestError('active branch changed after candidate preparation')
    if repository.original_sha != candidate.original_sha:
        raise ManifestError('active HEAD changed after candidate preparation')
    if not repository.clean:
        raise ManifestError('active worktree changed after candidate preparation')
    resolved_candidate_sha = _run_git(
        manifest.worktree,
        'rev-parse',
        f'{candidate.candidate_sha}^{{commit}}',
    )
    if resolved_candidate_sha != candidate.candidate_sha:
        raise ManifestError('candidate commit no longer resolves to the verified SHA')

    target = manifest.installed_app
    backup = target.with_name(f'.{target.stem}.rollback-{candidate.candidate_id}.app')
    staged = target.with_name(f'.{target.stem}.install-{candidate.candidate_id}.app')
    transaction_path = candidate.state_dir / 'transaction.json'
    if _path_lexists(candidate.state_dir / 'install.json') or _path_lexists(
        candidate.state_dir / 'recovery.json'
    ):
        raise ManifestError('pre-existing install or recovery report blocks this update')
    if _path_lexists(transaction_path):
        raise ManifestError('pre-existing install transaction blocks this update')
    if _path_lexists(backup) or _path_lexists(staged):
        raise ManifestError('stale install or rollback bundle blocks this update')

    original_artifact_sha256 = _hash_artifact(target)
    if original_artifact_sha256 == verified.artifact_sha256:
        raise ManifestError('candidate app bundle is identical to the installed app bundle')
    _write_immutable_json(
        transaction_path,
        {
            'schema': 1,
            'status': 'installing',
            'candidate_id': candidate.candidate_id,
            'manifest_sha256': current_manifest_sha256,
            'original_sha': candidate.original_sha,
            'candidate_sha': candidate.candidate_sha,
            'target': str(target),
            'staged': str(staged),
            'backup': str(backup),
            'original_artifact_sha256': original_artifact_sha256,
            'candidate_artifact_sha256': verified.artifact_sha256,
            'started_at': _next_install_started_at(candidate.state_dir.parent),
        },
    )

    safety_ref = f'refs/hermes-managed-update/safety/{candidate.candidate_id}'
    try:
        copy_bundle(verified.artifact, staged)
        if _hash_artifact(staged) != verified.artifact_sha256:
            raise ManifestError('staged app hash does not match the verified artifact')

        _run_git(
            manifest.worktree,
            'update-ref',
            safety_ref,
            candidate.original_sha,
        )
        _run_git(manifest.worktree, 'reset', '--keep', candidate.candidate_sha)
        promoted_repository = inspect_repository(manifest)
        if (
            promoted_repository.original_sha != candidate.candidate_sha
            or not promoted_repository.clean
        ):
            raise ManifestError('active worktree drifted while promoting the candidate')

        _swap_app_bundle(target, staged, backup, health_check)

        result = InstallResult(
            candidate_id=candidate.candidate_id,
            status='installed',
            safety_ref=safety_ref,
            backup_path=backup,
        )
        report = {
            'schema': 1,
            'candidate_id': result.candidate_id,
            'status': result.status,
            'safety_ref': result.safety_ref,
            'original_sha': candidate.original_sha,
            'candidate_sha': candidate.candidate_sha,
            'artifact_sha256': verified.artifact_sha256,
        }
        _write_immutable_json(candidate.state_dir / 'install.json', report)
        return result
    except Exception as error:
        try:
            _recover_install_transaction_locked(manifest, candidate.state_dir)
        except Exception as recovery_error:
            raise ManifestError(
                f'candidate installation failed ({error}); transaction recovery failed: {recovery_error}'
            ) from error
        if isinstance(error, ManifestError):
            raise
        raise ManifestError(f'candidate installation failed: {error}') from error


def install_verified_candidate(
    manifest: ManagedUpdateManifest,
    candidate: CandidateResult,
    verified: VerifiedCandidate,
    *,
    confirmation_token: str,
    confirmed_candidate_sha: str,
    confirmed_artifact_sha256: str,
    copy_bundle: Callable[[Path, Path], None] = _default_copy_bundle,
    health_check: Callable[[Path], bool],
) -> InstallResult:
    with _repository_update_lock(manifest):
        return _install_verified_candidate_locked(
            manifest,
            candidate,
            verified,
            confirmation_token=confirmation_token,
            confirmed_candidate_sha=confirmed_candidate_sha,
            confirmed_artifact_sha256=confirmed_artifact_sha256,
            copy_bundle=copy_bundle,
            health_check=health_check,
        )


def _fetch_configured_upstream(manifest: ManagedUpdateManifest) -> None:
    if '/' not in manifest.upstream:
        raise ManifestError('upstream must use remote/branch form')
    remote, branch = manifest.upstream.split('/', 1)
    if not remote or not branch:
        raise ManifestError('upstream must use remote/branch form')
    _run_git(
        manifest.worktree,
        'fetch',
        '--prune',
        remote,
        f'refs/heads/{branch}:refs/remotes/{remote}/{branch}',
    )


def _emit_event(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(',', ':')), flush=True)


def _read_install_approval_fd(descriptor: int) -> tuple[str, str, str, str]:
    if descriptor < 3:
        raise ManifestError('approval descriptor must be an inherited non-standard descriptor')
    descriptor_stat = os.fstat(descriptor)
    if not (stat.S_ISFIFO(descriptor_stat.st_mode) or stat.S_ISSOCK(descriptor_stat.st_mode)):
        raise ManifestError('approval descriptor must refer to an inherited pipe or socket')
    received = bytearray()
    try:
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            received.extend(chunk)
            if len(received) > 8192:
                raise ManifestError('approval capability payload is oversized')
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(received.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError('approval capability payload is not valid JSON') from error
    if not isinstance(payload, dict) or set(payload) != {
        'confirmation_token',
        'candidate_sha',
        'artifact_sha256',
        'verification_report_sha256',
    }:
        raise ManifestError('approval capability payload has unexpected fields')
    confirmation_token = _required_string(payload, 'confirmation_token')
    candidate_sha = _required_string(payload, 'candidate_sha')
    artifact_sha256 = _required_string(payload, 'artifact_sha256')
    verification_report_sha256 = _required_string(payload, 'verification_report_sha256')
    if not re.fullmatch(r'[0-9a-f]{64}', confirmation_token):
        raise ManifestError('approval capability token has an invalid format')
    if not re.fullmatch(r'[0-9a-f]{40}', candidate_sha):
        raise ManifestError('approval capability candidate SHA has an invalid format')
    if not re.fullmatch(r'[0-9a-f]{64}', artifact_sha256):
        raise ManifestError('approval capability artifact hash has an invalid format')
    if not re.fullmatch(r'[0-9a-f]{64}', verification_report_sha256):
        raise ManifestError('approval capability verification report hash has an invalid format')
    return confirmation_token, candidate_sha, artifact_sha256, verification_report_sha256


def _write_installer_ready_fd(descriptor: int, candidate_id: str) -> None:
    if descriptor < 3:
        raise ManifestError('ready descriptor must be an inherited non-standard descriptor')
    descriptor_stat = os.fstat(descriptor)
    if not (stat.S_ISFIFO(descriptor_stat.st_mode) or stat.S_ISSOCK(descriptor_stat.st_mode)):
        raise ManifestError('ready descriptor must refer to an inherited pipe or socket')
    encoded = (
        json.dumps(
            {'event': 'installer-ready', 'candidate_id': candidate_id, 'pid': os.getpid()},
            sort_keys=True,
            separators=(',', ':'),
        )
        + '\n'
    ).encode('utf-8')
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
    finally:
        os.close(descriptor)


def _wait_for_process_exit(pid: int, timeout_seconds: float = 30.0) -> None:
    if pid <= 0 or pid == os.getpid():
        raise ManifestError('wait-pid must identify another live process')
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError as error:
            raise ManifestError(f'cannot inspect process {pid}: {error}') from error
        time.sleep(0.1)
    raise ManifestError(f'timed out waiting for process {pid} to exit')


def _terminate_failed_candidate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _installed_app_health_check(
    target: Path,
    *,
    timeout_seconds: float = 120,
    stabilization_seconds: float = 2,
) -> bool:
    info_plist = target / 'Contents' / 'Info.plist'
    executable_dir = target / 'Contents' / 'MacOS'
    if not info_plist.is_file() or not executable_dir.is_dir():
        return False
    try:
        with info_plist.open('rb') as handle:
            plist = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return False
    executable_name = plist.get('CFBundleExecutable')
    if not isinstance(executable_name, str) or not executable_name or '/' in executable_name:
        return False
    executable = (executable_dir / executable_name).resolve(strict=True)
    if executable_dir.resolve() not in executable.parents or not executable.is_file():
        return False

    codesign = Path('/usr/bin/codesign')
    if codesign.exists():
        verified = subprocess.run(
            [str(codesign), '--verify', '--deep', '--strict', str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if verified.returncode != 0:
            return False

    read_fd, write_fd = os.pipe()
    os.set_inheritable(write_fd, True)
    process: subprocess.Popen[bytes] | None = None
    healthy = False
    received = bytearray()
    try:
        process = subprocess.Popen(
            [
                str(executable),
                '--managed-update-health-fd',
                str(write_fd),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(write_fd,),
            start_new_session=True,
        )
        os.close(write_fd)
        write_fd = -1
        os.set_blocking(read_fd, False)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            readable, _, _ = select.select([read_fd], [], [], 0.1)
            if not readable:
                continue
            try:
                chunk = os.read(read_fd, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                return False
            received.extend(chunk)
            if len(received) > 4096:
                return False
            if b'\n' not in received:
                continue
            line, remainder = bytes(received).split(b'\n', 1)
            if remainder.strip():
                return False
            try:
                payload = json.loads(line.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False
            if payload.get('pid') != process.pid:
                return False
            stable_until = min(deadline, time.monotonic() + stabilization_seconds)
            while time.monotonic() < stable_until:
                if process.poll() is not None:
                    return False
                time.sleep(0.1)
            healthy = process.poll() is None
            return healthy
        return False
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        if process is not None and not healthy:
            _terminate_failed_candidate(process)


def main(argv: list[str] | None = None) -> int:
    default_manifest = (
        Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes'))
        / 'customizations'
        / 'managed-update'
        / 'manifest.json'
    )
    parser = argparse.ArgumentParser(prog='hermes-managed-update')
    parser.add_argument('--manifest', type=Path, default=default_manifest)
    commands = parser.add_subparsers(dest='command', required=True)
    check_parser = commands.add_parser('check')
    check_parser.add_argument('--no-fetch', action='store_true')
    recover_parser = commands.add_parser('recover')
    recover_parser.add_argument('--state-root', type=Path, required=True)
    prepare_parser = commands.add_parser('prepare')
    prepare_parser.add_argument('--state-root', type=Path, required=True)
    prepare_parser.add_argument('--candidate-id', required=True)
    prepare_parser.add_argument('--no-fetch', action='store_true')
    accept_review_parser = commands.add_parser('accept-review')
    accept_review_parser.add_argument('--state-root', type=Path, required=True)
    accept_review_parser.add_argument('--candidate-id', required=True)
    resume_conflict_parser = commands.add_parser('resume-conflict')
    resume_conflict_parser.add_argument('--state-root', type=Path, required=True)
    resume_conflict_parser.add_argument('--candidate-id', required=True)
    cancel_parser = commands.add_parser('cancel')
    cancel_parser.add_argument('--state-root', type=Path, required=True)
    cancel_parser.add_argument('--candidate-id', required=True)
    install_parser = commands.add_parser('install')
    install_parser.add_argument('--state-root', type=Path, required=True)
    install_parser.add_argument('--candidate-id', required=True)
    install_parser.add_argument('--approval-fd', type=int, required=True)
    install_parser.add_argument('--ready-fd', type=int, required=True)
    install_parser.add_argument('--wait-pid', type=int)
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest)
        if args.command == 'check':
            if not args.no_fetch:
                _fetch_configured_upstream(manifest)
            status = inspect_repository(manifest)
            _emit_event(
                {
                    'event': 'check-complete',
                    'branch': status.current_branch,
                    'original_sha': status.original_sha,
                    'upstream_sha': status.upstream_sha,
                    'ahead': status.ahead,
                    'behind': status.behind,
                    'clean': status.clean,
                    'custom_commits': len(status.custom_commit_subjects),
                    'custom_commit_subjects': list(status.custom_commit_subjects),
                }
            )
            return 0
        if args.command == 'recover':
            recovered = recover_pending_install_transactions(manifest, args.state_root)
            _emit_event(
                {
                    'event': 'recovery-complete',
                    'recovered': len(recovered),
                    'reports': [str(path) for path in recovered],
                }
            )
            return 0
        if args.command == 'prepare':
            if not args.no_fetch:
                _fetch_configured_upstream(manifest)
            candidate = prepare_candidate(
                manifest,
                args.state_root,
                candidate_id=args.candidate_id,
            )
            if candidate.status != 'ready':
                _emit_event(
                    {
                        'event': 'candidate-decision',
                        'candidate_id': candidate.candidate_id,
                        'status': candidate.status,
                        'original_sha': candidate.original_sha,
                        'upstream_sha': candidate.upstream_sha,
                        'candidate_sha': candidate.candidate_sha,
                        'conflicts': list(candidate.conflicts),
                        'recommendations': [
                            {
                                'feature_id': item.feature_id,
                                'kind': item.kind,
                                'commit_subject': item.commit_subject,
                            }
                            for item in candidate.recommendations
                        ],
                        'report': str(candidate.state_dir / 'report.json'),
                        'worktree': str(candidate.worktree),
                    }
                )
                return 0
            verified = verify_candidate(manifest, candidate)
            _emit_event(
                {
                    'event': 'candidate-ready',
                    'candidate_id': candidate.candidate_id,
                    'status': verified.status,
                    'original_sha': candidate.original_sha,
                    'upstream_sha': candidate.upstream_sha,
                    'candidate_sha': candidate.candidate_sha,
                    'artifact': str(verified.artifact),
                    'artifact_sha256': verified.artifact_sha256,
                    'verification_report_sha256': verified.verification_report_sha256,
                    'confirmation_token': approval_token(manifest, candidate, verified),
                    'report': str(candidate.state_dir / 'verification.json'),
                }
            )
            return 0
        if args.command == 'resume-conflict':
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', args.candidate_id):
                raise ManifestError('invalid candidate_id')
            state_root = args.state_root.resolve()
            state_dir = (state_root / args.candidate_id).resolve()
            if state_dir.parent != state_root:
                raise ManifestError('candidate state directory escapes state root')
            conflict_candidate = load_candidate(state_dir)
            resolution_path = state_dir / 'conflict-resolution.json'
            verification_path = state_dir / 'verification.json'
            if conflict_candidate.status == 'conflict':
                candidate = resume_candidate_conflict(manifest, conflict_candidate)
            elif (
                conflict_candidate.status == 'ready'
                and _path_lexists(resolution_path)
                and not _path_lexists(verification_path)
            ):
                # A prior process may have durably recorded the resolution and then
                # exited before verification. load_candidate already authenticates the
                # immutable resolution, so retry only the missing verification phase.
                candidate = conflict_candidate
            else:
                raise ManifestError(
                    'candidate conflict cannot be resumed from its current durable state'
                )
            verified = verify_candidate(manifest, candidate)
            _emit_event(
                {
                    'event': 'candidate-ready',
                    'candidate_id': candidate.candidate_id,
                    'status': verified.status,
                    'original_sha': candidate.original_sha,
                    'upstream_sha': candidate.upstream_sha,
                    'candidate_sha': candidate.candidate_sha,
                    'artifact': str(verified.artifact),
                    'artifact_sha256': verified.artifact_sha256,
                    'verification_report_sha256': verified.verification_report_sha256,
                    'confirmation_token': approval_token(manifest, candidate, verified),
                    'report': str(candidate.state_dir / 'verification.json'),
                }
            )
            return 0
        if args.command == 'accept-review':
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', args.candidate_id):
                raise ManifestError('invalid candidate_id')
            state_root = args.state_root.resolve()
            state_dir = (state_root / args.candidate_id).resolve()
            if state_dir.parent != state_root:
                raise ManifestError('candidate state directory escapes state root')
            candidate = load_candidate(state_dir)
            verified = accept_candidate_review(manifest, candidate)
            _emit_event(
                {
                    'event': 'candidate-ready',
                    'candidate_id': candidate.candidate_id,
                    'status': verified.status,
                    'original_sha': candidate.original_sha,
                    'upstream_sha': candidate.upstream_sha,
                    'candidate_sha': candidate.candidate_sha,
                    'artifact': str(verified.artifact),
                    'artifact_sha256': verified.artifact_sha256,
                    'verification_report_sha256': verified.verification_report_sha256,
                    'confirmation_token': approval_token(manifest, candidate, verified),
                    'report': str(candidate.state_dir / 'verification.json'),
                }
            )
            return 0
        if args.command == 'cancel':
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', args.candidate_id):
                raise ManifestError('invalid candidate_id')
            state_root = args.state_root.resolve()
            state_dir = (state_root / args.candidate_id).resolve()
            if state_dir.parent != state_root:
                raise ManifestError('candidate state directory escapes state root')
            invalidate_candidate_state(state_dir, args.candidate_id)
            candidate = load_candidate(state_dir, allow_cancelled=True)
            report = cancel_candidate(manifest, candidate)
            _emit_event(
                {
                    'event': 'candidate-cancelled',
                    'candidate_id': candidate.candidate_id,
                    'status': 'cancelled',
                    'report': str(report),
                }
            )
            return 0
        if args.command == 'install':
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', args.candidate_id):
                raise ManifestError('invalid candidate_id')
            state_root = args.state_root.resolve()
            state_dir = (state_root / args.candidate_id).resolve()
            if state_dir.parent != state_root:
                raise ManifestError('candidate state directory escapes state root')
            (
                confirmation_token,
                confirmed_candidate_sha,
                confirmed_artifact_sha256,
                confirmed_verification_report_sha256,
            ) = _read_install_approval_fd(args.approval_fd)
            candidate = load_candidate(state_dir)
            verified = load_verified_candidate(state_dir, candidate)
            if not hmac.compare_digest(
                confirmed_verification_report_sha256,
                verified.verification_report_sha256,
            ):
                raise ManifestError('approved verification report was replaced after user confirmation')
            validate_install_approval(
                manifest,
                candidate,
                verified,
                confirmation_token=confirmation_token,
                confirmed_candidate_sha=confirmed_candidate_sha,
                confirmed_artifact_sha256=confirmed_artifact_sha256,
            )
            if not hmac.compare_digest(
                _hash_artifact(verified.artifact),
                verified.artifact_sha256,
            ):
                raise ManifestError('verified artifact changed before installer readiness')
            _write_installer_ready_fd(args.ready_fd, candidate.candidate_id)
            if args.wait_pid is not None:
                _wait_for_process_exit(args.wait_pid)
            installed = install_verified_candidate(
                manifest,
                candidate,
                verified,
                confirmation_token=confirmation_token,
                confirmed_candidate_sha=confirmed_candidate_sha,
                confirmed_artifact_sha256=confirmed_artifact_sha256,
                health_check=_installed_app_health_check,
            )
            _emit_event(
                {
                    'event': 'install-complete',
                    'candidate_id': installed.candidate_id,
                    'status': installed.status,
                    'safety_ref': installed.safety_ref,
                    'report': str(candidate.state_dir / 'install.json'),
                }
            )
            return 0
        raise ManifestError(f'unsupported command: {args.command}')
    except Exception as error:
        _emit_event(
            {
                'event': 'error',
                'error': error.__class__.__name__,
                'message': str(error),
            }
        )
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
