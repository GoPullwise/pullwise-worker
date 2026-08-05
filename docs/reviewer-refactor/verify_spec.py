from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any


SPEC_DIR = Path('docs/reviewer-refactor')
MANIFEST_REL = SPEC_DIR / 'spec-manifest.json'
MAIN_REL = Path('docs/codex-sdk-reviewer-skill-worker-refactor-proposal.md')
SAFE_INT_MAX = 9_007_199_254_740_991

REQUIRED_FILES = (
    MAIN_REL.as_posix(),
    'docs/reviewer-refactor/authority-and-readiness.md',
    'docs/reviewer-refactor/bootstrap-command.json',
    'docs/reviewer-refactor/evidence-and-determinism.md',
    'docs/reviewer-refactor/execution-card.schema.json',
    'docs/reviewer-refactor/execution-cards.json',
    'docs/reviewer-refactor/fixtures/spec-verifier/invalid/dependency-cycle.json',
    'docs/reviewer-refactor/fixtures/spec-verifier/invalid/float-confidence.json',
    'docs/reviewer-refactor/fixtures/spec-verifier/invalid/path-escape.json',
    'docs/reviewer-refactor/fixtures/spec-verifier/manifest.json',
    'docs/reviewer-refactor/fixtures/spec-verifier/valid/scalar-profile.json',
    'docs/reviewer-refactor/operations-and-execution.md',
    'docs/reviewer-refactor/readiness.json',
    'docs/reviewer-refactor/runtime-contract-and-security.md',
    'docs/reviewer-refactor/skill-context-and-evaluation.md',
    'docs/reviewer-refactor/verify_spec.py',
)

CARD_IDS = (
    'GOV-0A', 'EVD-0', 'GOV-0B', 'EVD-1', 'CON-0', 'BEN-0',
    'SKILL-1', 'RUN-1', 'RUN-2', 'RES-1', 'PUB-1', 'BEN-1',
    'SRV-1', 'CON-1', 'SRV-2', 'WEB-1', 'ADM-1', 'CUT-1',
)

GATE_IDS = tuple(
    f'SPEC-READY-{index:02d}-{name}'
    for index, name in enumerate(
        ('AUTHORITY', 'INSTRUCTIONS', 'MANIFEST', 'BOOTSTRAP', 'EVIDENCE',
         'CONTRACT', 'SECURITY', 'SKILL', 'CONTEXT', 'CAPABILITY',
         'RELEASE', 'EXECUTION'),
        start=1,
    )
)

CARD_KEYS = {
    'schema_id', 'id', 'title', 'stage', 'authority_state', 'owner_role',
    'reviewer_roles', 'repositories', 'write_set', 'forbidden_set',
    'dependencies', 'decision_bindings', 'input_artifacts',
    'blocking_predicates', 'red_commands', 'green_commands',
    'focused_commands', 'full_commands', 'ci_commands', 'outputs',
    'rollback', 'pass_predicates', 'line_policy',
}


class SpecError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f'{code}: {detail}')
        self.code = code
        self.detail = detail


def fail(code: str, detail: str) -> None:
    raise SpecError(code, detail)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def no_float(value: str) -> Any:
    fail('json.float_forbidden', value)


def no_constant(value: str) -> Any:
    fail('json.constant_forbidden', value)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail('json.duplicate_key', key)
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        fail('json.read_failed', f'{path}: {exc}')
    try:
        return json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_float=no_float,
            parse_constant=no_constant,
        )
    except SpecError:
        raise
    except json.JSONDecodeError as exc:
        fail('json.invalid', f'{path}:{exc.lineno}:{exc.colno}')


def validate_profile(value: Any, location: str = '$') -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > SAFE_INT_MAX:
            fail('json.unsafe_integer', location)
        return
    if isinstance(value, str):
        if unicodedata.normalize('NFC', value) != value:
            fail('json.non_nfc_string', location)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_profile(item, f'{location}[{index}]')
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not key.isascii():
                fail('json.non_ascii_key', f'{location}.{key}')
            validate_profile(item, f'{location}.{key}')
        return
    fail('json.unsupported_type', location)


def canonical_rel(value: str) -> str:
    if not value or '\\' in value or value.startswith('/'):
        fail('path.noncanonical', value)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ('', '.', '..') for part in path.parts):
        fail('path.noncanonical', value)
    if path.as_posix() != value or unicodedata.normalize('NFC', value) != value:
        fail('path.noncanonical', value)
    return value


def exact_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    actual = set(value)
    if actual != expected:
        fail('schema.keys', f'{location}: missing={sorted(expected-actual)} extra={sorted(actual-expected)}')


def verify_manifest(root: Path) -> dict[str, Any]:
    manifest = load_json(root / MANIFEST_REL)
    validate_profile(manifest)
    exact_keys(manifest, {'schema_id', 'spec_id', 'spec_version', 'status', 'files'}, 'manifest')
    if manifest['schema_id'] != 'reviewer-refactor-spec-manifest/v1':
        fail('manifest.schema_id', str(manifest['schema_id']))
    if manifest['spec_id'] != 'pullwise-reviewer-refactor/v1':
        fail('manifest.spec_id', str(manifest['spec_id']))
    if manifest['status'] != 'PROPOSED_INERT':
        fail('manifest.status', str(manifest['status']))
    files = manifest['files']
    if not isinstance(files, list):
        fail('manifest.files_type', type(files).__name__)
    paths: list[str] = []
    folded: set[str] = set()
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            fail('manifest.entry_type', str(index))
        exact_keys(entry, {'path', 'size_bytes', 'sha256', 'media_type', 'role'}, f'manifest.files[{index}]')
        rel = canonical_rel(entry['path'])
        if rel == MANIFEST_REL.as_posix():
            fail('manifest.self_reference', rel)
        if rel.casefold() in folded:
            fail('manifest.case_collision', rel)
        folded.add(rel.casefold())
        paths.append(rel)
        target = root / Path(rel)
        if not target.is_file() or target.is_symlink():
            fail('manifest.file_missing_or_unsafe', rel)
        data = target.read_bytes()
        if len(data) != entry['size_bytes']:
            fail('manifest.size_mismatch', rel)
        if sha256(data) != entry['sha256']:
            fail('manifest.hash_mismatch', rel)
    if paths != sorted(paths, key=lambda item: item.encode('utf-8')):
        fail('manifest.path_order', 'files must use UTF-8 byte order')
    if tuple(paths) != tuple(sorted(REQUIRED_FILES, key=lambda item: item.encode('utf-8'))):
        fail('manifest.closed_set', f'expected={len(REQUIRED_FILES)} actual={len(paths)}')
    return manifest


def verify_readiness(root: Path) -> None:
    value = load_json(root / SPEC_DIR / 'readiness.json')
    validate_profile(value)
    exact_keys(value, {'schema_id', 'spec_id', 'spec_version', 'activation_state', 'overall_status', 'reason_codes', 'gates'}, 'readiness')
    if value['activation_state'] != 'PROPOSED_INERT' or value['overall_status'] != 'NOT_AUTHORIZED':
        fail('readiness.current_state', 'candidate must remain inert and unauthorized')
    ids = [gate.get('gate_id') for gate in value['gates']]
    if tuple(ids) != GATE_IDS:
        fail('readiness.gate_set', repr(ids))
    for gate in value['gates']:
        exact_keys(gate, {'gate_id', 'status', 'reason_code', 'evidence_refs'}, gate['gate_id'])
    by_id = {gate['gate_id']: gate for gate in value['gates']}
    if by_id['SPEC-READY-04-BOOTSTRAP']['status'] != 'FAIL':
        fail('readiness.bootstrap_truth', 'missing collector must remain FAIL')


def graph_has_cycle(graph: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for dependency in graph.get(node, []):
            if visit(dependency):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def path_overlap(left: dict[str, str], right: dict[str, str]) -> bool:
    if left['repo_id'] != right['repo_id']:
        return False
    a = PurePosixPath(left['path']).parts
    b = PurePosixPath(right['path']).parts
    common = min(len(a), len(b))
    if a[:common] != b[:common]:
        return False
    if len(a) == len(b):
        return True
    shorter = left if len(a) < len(b) else right
    return shorter['path_kind'] == 'tree'


def ancestors(graph: dict[str, list[str]], node: str) -> set[str]:
    result: set[str] = set()
    stack = list(graph[node])
    while stack:
        current = stack.pop()
        if current not in result:
            result.add(current)
            stack.extend(graph[current])
    return result


def verify_cards(root: Path, main_text: str) -> None:
    value = load_json(root / SPEC_DIR / 'execution-cards.json')
    validate_profile(value)
    exact_keys(value, {'schema_id', 'spec_id', 'generation', 'cards'}, 'cards-root')
    if value['schema_id'] != 'reviewer-refactor-execution-cards/v1' or value['generation'] != 1:
        fail('cards.identity', 'unexpected schema or generation')
    cards = value['cards']
    ids = tuple(card.get('id') for card in cards)
    if ids != CARD_IDS:
        fail('cards.id_order', repr(ids))
    graph: dict[str, list[str]] = {}
    card_by_id: dict[str, dict[str, Any]] = {}
    for card in cards:
        exact_keys(card, CARD_KEYS, f'card:{card.get("id")}')
        card_id = card['id']
        card_by_id[card_id] = card
        graph[card_id] = card['dependencies']
        if card['schema_id'] != 'reviewer-refactor-execution-card/v1':
            fail('cards.schema_id', card_id)
        if card['authority_state'] != 'NOT_AUTHORIZED':
            fail('cards.current_authority', card_id)
        if not card['blocking_predicates'] or not card['pass_predicates']:
            fail('cards.empty_predicates', card_id)
        for name in ('red_commands', 'green_commands', 'focused_commands', 'full_commands', 'ci_commands'):
            if card[name]:
                fail('cards.unauthorized_command', f'{card_id}:{name}')
        if card['line_policy'] != {'default_max': 400, 'review_max': 600, 'hard_max': 600}:
            fail('cards.line_policy', card_id)
        for item in card['write_set']:
            exact_keys(item, {'repo_id', 'path', 'path_kind'}, f'{card_id}.write_set')
            canonical_rel(item['path'])
        for dependency in card['dependencies']:
            if dependency not in card_by_id:
                fail('cards.dependency_order_or_unknown', f'{card_id}->{dependency}')
        if f'| {card_id} |' not in main_text:
            fail('cards.main_ledger_missing', card_id)
    if graph_has_cycle(graph):
        fail('cards.dependency_cycle', 'execution cards')
    reach = {card_id: ancestors(graph, card_id) for card_id in CARD_IDS}
    for index, left_id in enumerate(CARD_IDS):
        for right_id in CARD_IDS[index + 1:]:
            if left_id in reach[right_id] or right_id in reach[left_id]:
                continue
            for left_path in card_by_id[left_id]['write_set']:
                for right_path in card_by_id[right_id]['write_set']:
                    if path_overlap(left_path, right_path):
                        fail('cards.parallel_write_overlap', f'{left_id}<->{right_id}')


def verify_prose(root: Path) -> str:
    main = (root / MAIN_REL).read_text(encoding='utf-8')
    normative = [main]
    for name in (
        'authority-and-readiness.md', 'evidence-and-determinism.md',
        'runtime-contract-and-security.md', 'skill-context-and-evaluation.md',
        'operations-and-execution.md',
    ):
        normative.append((root / SPEC_DIR / name).read_text(encoding='utf-8'))
    joined = '\n'.join(normative)
    if 'pullwise-reviewer-refactor/v1' not in main or 'CANDIDATE_NOT_ACTIVE' not in main:
        fail('prose.identity', 'main metadata missing')
    if re.search(r'numeric confidence\s*`?\[0,1\]`?', joined, re.IGNORECASE):
        fail('prose.float_confidence', 'legacy confidence contract remains')
    if 'pullwise-server/contracts/reviewer-worker/v2/' in joined:
        fail('prose.split_contract_root', 'legacy split canonical root remains')
    for required in ('stable-projection/v1', 'CONTEXT_BUDGET_EXCEEDED', 'instruction_effect_denied', 'K = (C * p + 99) // 100'):
        if required not in joined:
            fail('prose.required_rule_missing', required)
    return main


def verify_fixtures(root: Path) -> None:
    base = root / SPEC_DIR / 'fixtures/spec-verifier'
    manifest = load_json(base / 'manifest.json')
    validate_profile(manifest)
    valid = load_json(base / 'valid/scalar-profile.json')
    validate_profile(valid)
    if valid.get('confidence_bps') != 7000:
        fail('fixture.valid_scalar', 'confidence_bps')
    try:
        load_json(base / 'invalid/float-confidence.json')
    except SpecError as exc:
        if exc.code != 'json.float_forbidden':
            raise
    else:
        fail('fixture.float_not_rejected', 'invalid float fixture')
    cycle = load_json(base / 'invalid/dependency-cycle.json')
    graph = {card['id']: card['dependencies'] for card in cycle['cards']}
    if not graph_has_cycle(graph):
        fail('fixture.cycle_not_detected', 'invalid dependency fixture')
    escaped = load_json(base / 'invalid/path-escape.json')
    try:
        canonical_rel(escaped['path'])
    except SpecError as exc:
        if exc.code != 'path.noncanonical':
            raise
    else:
        fail('fixture.path_not_rejected', 'invalid path fixture')


def verify(root: Path) -> dict[str, Any]:
    root = root.resolve()
    manifest = verify_manifest(root)
    main = verify_prose(root)
    verify_readiness(root)
    verify_cards(root, main)
    verify_fixtures(root)
    return {
        'schema_id': 'reviewer-refactor-spec-verification/v1',
        'status': 'PASS',
        'spec_id': manifest['spec_id'],
        'spec_version': manifest['spec_version'],
        'file_count': len(manifest['files']),
        'card_count': len(CARD_IDS),
        'gate_count': len(GATE_IDS),
    }


def self_test(root: Path) -> dict[str, Any]:
    result = verify(root)
    with tempfile.TemporaryDirectory(prefix='reviewer-refactor-spec-') as directory:
        temp_root = Path(directory)
        manifest_path = root / MANIFEST_REL
        manifest = load_json(manifest_path)
        for entry in manifest['files']:
            source = root / entry['path']
            target = temp_root / entry['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        target_manifest = temp_root / MANIFEST_REL
        target_manifest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(manifest_path, target_manifest)
        tampered = temp_root / 'docs/reviewer-refactor/authority-and-readiness.md'
        tampered.write_bytes(tampered.read_bytes() + b'\n')
        try:
            verify(temp_root)
        except SpecError as exc:
            if exc.code != 'manifest.size_mismatch':
                raise
        else:
            fail('self_test.tamper_not_detected', str(tampered))
    result['self_test'] = 'PASS'
    return result


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo-root', default='.')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    try:
        result = self_test(Path(args.repo_root)) if args.self_test else verify(Path(args.repo_root))
    except SpecError as exc:
        emit({'schema_id': 'reviewer-refactor-spec-verification/v1', 'status': 'FAIL', 'reason_code': exc.code, 'detail': exc.detail})
        return 1
    except Exception as exc:
        emit({'schema_id': 'reviewer-refactor-spec-verification/v1', 'status': 'INDETERMINATE', 'reason_code': 'verifier.unhandled', 'detail': type(exc).__name__})
        return 2
    emit(result)
    return 0


if __name__ == '__main__':
    sys.exit(main())
