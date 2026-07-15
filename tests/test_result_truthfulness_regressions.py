from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pullwise_worker.review_worker_v1 import (
    ReviewWorkerV1,
    ensure_repository_mirror,
    phase_progress_data,
    render_markdown,
    repair_agent_report_artifact,
    result_artifact_manifest_items,
    write_json,
    write_uploaded_artifact_manifest,
)


def artifact_item(artifact_id: str, name: str, *, required: bool) -> dict:
    return {
        'artifact_id': artifact_id,
        'name': name,
        'required': required,
        'media_type': 'application/json',
        'role': artifact_id,
        'size_bytes': 2,
        'sha256': '0' * 64,
    }


def finding(finding_id: str, title: str) -> dict:
    return {
        'id': finding_id,
        'title': title,
        'severity': 'high',
        'confidence': 0.9,
        'locations': [{'path': 'app.py', 'start_line': 1, 'end_line': 1}],
        'evidence': [{'summary': 'The failing branch is reachable.'}],
        'impact': 'A request returns the wrong value.',
        'recommendation': 'Guard the branch before reading state.',
        'next_agent_task': f'Fix {title}',
    }


class ResultTruthfulnessRegressionTests(unittest.TestCase):
    def test_result_envelope_reports_actual_bundle_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / 'repo' / '.codex-review' / 'runs' / 'run-1'
            artifact_dir = root / 'artifacts' / 'run-1'
            run_dir.mkdir(parents=True)
            artifact_dir.mkdir(parents=True)
            write_json(
                run_dir / 'bundle-plan.json',
                {
                    'schema_version': 'bundle-plan/v1',
                    'bundles': [
                        {'bundle_id': 'b1'},
                        {'bundle_id': 'b2'},
                        {'bundle_id': 'b3'},
                    ],
                },
            )
            write_json(
                artifact_dir / 'artifact-manifest.json',
                {
                    'schema_version': 'artifact-manifest/v1',
                    'run_id': 'run-1',
                    'items': [],
                },
            )
            worker = ReviewWorkerV1(
                SimpleNamespace(worker_id='wk-1', service_home=str(root)),
                client=object(),
            )

            envelope = worker.build_envelope(
                {'job_id': 'job-1', 'run_id': 'run-1'},
                'run-1',
                'completed',
                1.0,
                artifact_dir,
                run_dir,
            )

        self.assertEqual(envelope['extensions']['worker_internal']['bundle_count'], 3)

    def test_uploaded_snapshot_is_the_exact_result_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / 'run-1'
            artifact_dir.mkdir()
            required = artifact_item('report', 'report.json', required=True)
            optional = artifact_item('trace', 'trace.json', required=False)
            manifest = {
                'schema_version': 'artifact-manifest/v1',
                'run_id': 'run-1',
                'items': [required, optional],
            }
            write_json(artifact_dir / 'artifact-manifest.json', manifest)
            write_uploaded_artifact_manifest(artifact_dir, manifest, [required])

            result = result_artifact_manifest_items(artifact_dir)

        self.assertEqual(result, [required])

    def test_upload_progress_distinguishes_manifest_total_from_uploaded_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / 'run-1'
            artifact_dir.mkdir()
            required = artifact_item('report', 'report.json', required=True)
            optional = artifact_item('trace', 'trace.json', required=False)
            (artifact_dir / 'report.json').write_text('{}', encoding='utf-8')
            (artifact_dir / 'trace.json').write_text('{}', encoding='utf-8')
            manifest = {
                'schema_version': 'artifact-manifest/v1',
                'run_id': 'run-1',
                'items': [required, optional],
            }
            write_json(artifact_dir / 'artifact-manifest.json', manifest)
            write_uploaded_artifact_manifest(artifact_dir, manifest, [required])

            progress = phase_progress_data(artifact_dir, 'upload_artifacts', artifact_dir)

        self.assertEqual(progress, {'artifacts_total': 2, 'artifacts_uploaded': 1})

    def test_one_validator_entry_cannot_back_multiple_main_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / 'run-1'
            run_dir.mkdir()
            write_json(run_dir / 'coverage.json', {'schema_version': 'coverage/v1'})
            write_json(
                run_dir / 'report.agent.json',
                {
                    'schema_id': 'codex-full-repo-review',
                    'schema_version': 'v1',
                    'summary': {'overall_risk': 'high', 'result_status': 'complete'},
                    'findings': [
                        finding('shared-id', 'First interpretation'),
                        finding('shared-id', 'Second interpretation'),
                    ],
                },
            )
            write_json(
                run_dir / 'validated-findings.json',
                {
                    'schema_version': 'validation-output/v1',
                    'validated_findings': [{'id': 'shared-id', 'status': 'confirmed'}],
                    'weak_findings': [],
                    'disproven_findings': [],
                },
            )

            repair_agent_report_artifact(run_dir, {'job_id': 'job-1', 'run_id': 'run-1'})
            report = __import__('json').loads(
                (run_dir / 'report.agent.json').read_text(encoding='utf-8')
            )

        self.assertEqual(len(report['findings']), 1)
        demoted = [item for item in report['appendix_findings'] if item.get('demoted_from_main_findings')]
        self.assertEqual(len(demoted), 1)

    def test_markdown_preserves_appendix_and_disproven_findings_and_counts_started_tests(self) -> None:
        markdown = render_markdown(
            {
                'commit_sha': 'abc123',
                'summary': {'overall_risk': 'unknown', 'result_status': 'complete'},
                'coverage': {},
                'findings': [],
                'appendix_findings': [finding('weak-1', 'Weak candidate retained')],
                'disproven_findings': [finding('no-1', 'Disproven candidate retained')],
                'intent_test_validation': {
                    'test_results': [
                        {'test_id': 'skipped', 'status': 'skipped', 'skip_reason': 'tool unavailable'},
                        {'test_id': 'started', 'status': 'passed'},
                    ]
                },
            }
        )

        self.assertIn('Intent tests run: 1', markdown)
        self.assertIn('## Appendix Findings', markdown)
        self.assertIn('Weak candidate retained', markdown)
        self.assertIn('## Disproven Findings', markdown)
        self.assertIn('Disproven candidate retained', markdown)

    def test_repository_mirror_never_persists_clone_url_userinfo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mirror = Path(tmpdir) / 'mirror.git'
            commands: list[list[str]] = []

            def record(args: list[str], **_kwargs: object) -> None:
                commands.append(args)
                if args[:3] == ['git', 'init', '--bare']:
                    mirror.mkdir(parents=True, exist_ok=True)
                    (mirror / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')

            with patch('pullwise_worker.review_worker_v1.run_git', side_effect=record):
                ensure_repository_mirror(
                    mirror,
                    'https://alice:super-secret@example.com/acme/repo.git?token=also-secret',
                    env={},
                    deadline_monotonic=None,
                )

        remote = next(command for command in commands if 'remote' in command)
        self.assertEqual(remote[-1], 'https://example.com/acme/repo.git')
        self.assertNotIn('super-secret', ' '.join(remote))


if __name__ == '__main__':
    unittest.main()
