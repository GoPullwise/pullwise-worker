/* Pullwise reviewer contract consumer - npm (generated).
 *
 * Deterministically generated from the frozen pullwise-review/v1 manifest by
 * scripts/generate_reviewer_contract.py.  Build output: manual edits fail
 * scripts/check_reviewer_contract.py.  Regenerate with:
 *
 *     python scripts/generate_reviewer_contract.py --out generated
 *
 * Dependency-free ESM with named exports only.  Exposes the closed registries,
 * HTTP status mapping, and closed-object validators; SCHEMA is loaded from
 * ./schema.json.  The keyword subset matches the frozen
 * schema's full inventory ($ref internal, oneOf, type, const, enum, pattern,
 * min/maxLength, min/maximum, min/maxItems, items, required, properties,
 * additionalProperties).  Strict decoding is the caller's boundary.
 */
import SCHEMA_VALUE from './schema.json' with { type: 'json' };

export const CONTRACT_VERSION = "pullwise-review/v1";
export const CANONICALIZATION = "pullwise-canonical-json/v1";
export const MANIFEST_DIGEST = "sha256:71428f4dc199e7cbdbe99b64cbdeff03686cda59eb08e84f22224822f5a8167e";
export const FILES = {"canonical-json-v1.md":{"sha256":"46f7aaad5261291caa4452de3c622f7cf8a57b9ad41bfb58eb30826de9d8a0cf","size_bytes":1886},"fixtures/fixture-set.json":{"sha256":"353314490e1bdd6bebb7abbb58152a5a8cd0294e99b02fdf75293af44436d15f","size_bytes":1719},"fixtures/invalid/client-tenant-field.json":{"sha256":"7c76191a5f632675a9590fe3997e00c2f29512f26641e66120a81147a9cc7e83","size_bytes":292},"fixtures/invalid/duplicate-json-key.raw":{"sha256":"53689ae16293846ba5248a1c20044624c3b3808938cea7e982c7d403cf09088a","size_bytes":225},"fixtures/invalid/float-confidence.json":{"sha256":"bc6225e5152088d2cc0c5254667d6bc55609e938582931a5a1a5319035279668","size_bytes":843},"fixtures/invalid/path-traversal.json":{"sha256":"680c3b793aa6786b5cb0dc2664413dc7c10e3a51f55dc2fd7a38d92bec6a9f2f","size_bytes":846},"fixtures/invalid/unknown-enum.json":{"sha256":"8eda42406c81e5392826aa0be8cb411b2efd689d0e8eb584fdb55641f55001b2","size_bytes":249},"fixtures/valid/full-result-candidate.json":{"sha256":"8d8244527f5e9c152ff76e44428f929819a0bc55a1eeee5f828753f79b06e722","size_bytes":2246},"fixtures/valid/issue-status-command.json":{"sha256":"dd1851e67dd4eb6bbf3ada29a33d3b70cc49389ad22edcde9e3eda1713afc593","size_bytes":209},"fixtures/valid/minimal-scan.json":{"sha256":"bb3b9a1830c101bb0bce5183fe5ccad8739b18cfcbad5d6c84d9ffb8e2a23e3e","size_bytes":260},"openapi.json":{"sha256":"c5ddecfc85493ced176a66502dab743f0e0e5a88861853025a42827e4df15b0f","size_bytes":41891},"registry.json":{"sha256":"27eb9c5892506fc0ffba055fddfc8199adf674a3451c459a72c7687b397a8e34","size_bytes":5472},"shared/schemas/pullwise-review.schema.json":{"sha256":"39dd603502669542b9e16b30d60522796794014307ae0335b2f892d551f0c6dd","size_bytes":80741}};
export const REGISTRIES = {"artifact_kind":["REPORT","COVERAGE","RESTRICTED_DEBUG","AUDIT"],"artifact_state":["PREPARED","UPLOADED","COMMITTED","ABORTED"],"artifact_visibility":["PUBLIC","ADMIN","RESTRICTED"],"attempt_state":["PENDING","CLAIMED","PREPARING","RUNNING","VALIDATING","PUBLISHING","SUCCEEDED","RETRYABLE_FAILED","PERMANENT_FAILED","TIMED_OUT","CANCELLED","SUPERSEDED"],"cancellation_reason":["USER_REQUEST","DEADLINE_EXCEEDED","LEASE_LOST","WORKER_SHUTDOWN","POLICY_REVOKED"],"candidate_disposition":["RECEIVED","ACCEPTED","DUPLICATE","RETRY_SCHEDULED","REJECTED_STALE","REJECTED_INVALID","CONFLICT"],"candidate_kind":["RESULT","FAILURE_REPORT","CANCEL_ACK"],"coverage_gap_reason":["EXCLUDED_BY_POLICY","UNSUPPORTED_CONTENT","CONTEXT_BUDGET_EXCEEDED","VALIDATION_UNAVAILABLE","SOURCE_UNREADABLE","INSTRUCTION_CONFLICT","CANCELLED","RUNTIME_FAILURE"],"coverage_state":["REVIEWED","SKIPPED","UNAVAILABLE"],"error_code":["REQUEST_INVALID","AUTH_REQUIRED","AUTH_DENIED","RESOURCE_NOT_FOUND","REPOSITORY_NOT_AUTHORIZED","SOURCE_REF_NOT_FOUND","SOURCE_TOO_LARGE","SOURCE_UNSAFE","BUDGET_UNAVAILABLE","IDEMPOTENCY_CONFLICT","STATE_CONFLICT","VERSION_CONFLICT","WORKER_NOT_ATTESTED","LEASE_STALE","LEASE_EXPIRED","EVENT_SEQUENCE_CONFLICT","EVENT_DIGEST_CONFLICT","CANDIDATE_CONFLICT","TERMINAL_ALREADY_COMMITTED","CANDIDATE_INVALID","MODEL_OUTPUT_INVALID","EVIDENCE_INVALID","COVERAGE_INVALID","ARTIFACT_INVALID","ARTIFACT_NOT_COMMITTED","PROVIDER_UNAVAILABLE","LOCK_UNAVAILABLE","INTERNAL_ERROR"],"error_domain":["REQUEST","AUTH","SOURCE","BUDGET","LIFECYCLE","LEASE","EVENT","CANDIDATE","VALIDATION","ARTIFACT","PROVIDER","STORAGE","INTERNAL"],"event_type":["ATTEMPT_STARTED","SOURCE_READY","INSTRUCTIONS_READY","MODEL_STARTED","MODEL_COMPLETED","VALIDATION_STARTED","VALIDATION_COMPLETED","PUBLICATION_STARTED","HEARTBEAT","CANCEL_OBSERVED","ATTEMPT_FAILED"],"event_validation_outcome":["VALIDATED","REJECTED"],"finding_category":["CORRECTNESS","SECURITY","RELIABILITY","PERFORMANCE","DATA_INTEGRITY","AUTHORIZATION","CONCURRENCY","API_CONTRACT","TEST_GAP"],"issue_actor_class":["SYSTEM","USER","ADMIN"],"issue_status":["OPEN","FIXED","SNOOZED"],"lease_state":["ACTIVE","EXPIRED","REVOKED","RELEASED"],"model_finish_reason":["STOP","LENGTH","INTERRUPTED","ERROR"],"outbox_state":["PENDING","LEASED","DONE","DEAD"],"outbox_topic":["PUBLIC_SCAN","ADMIN_SCAN","FINDING_TO_ISSUE"],"progress_stage":["QUEUED","PREPARING","REVIEWING","VALIDATING","FINALIZING"],"reasoning_effort":["LOW","MEDIUM","HIGH","XHIGH"],"retry_class":["NEVER","POLICY","TRANSIENT"],"runtime_qualification_state":["UNQUALIFIED","PASS","FAIL","INDETERMINATE","QUARANTINED"],"scan_state":["QUEUED","RUNNING","FINALIZING","CANCEL_REQUESTED","COMPLETED","PARTIAL","FAILED","CANCELLED"],"severity":["CRITICAL","HIGH","MEDIUM","LOW"],"source_kind":["GITHUB_REPOSITORY_SNAPSHOT"],"terminal_outcome":["COMPLETED","PARTIAL","FAILED","CANCELLED"],"validation_status":["VALIDATED","UNVALIDATED","COUNTEREXAMPLE_REJECTED"]};
export const HTTP_STATUS_BY_ERROR_CODE = {"ARTIFACT_INVALID":422,"ARTIFACT_NOT_COMMITTED":409,"AUTH_DENIED":403,"AUTH_REQUIRED":401,"BUDGET_UNAVAILABLE":429,"CANDIDATE_CONFLICT":409,"CANDIDATE_INVALID":422,"COVERAGE_INVALID":422,"EVENT_DIGEST_CONFLICT":409,"EVENT_SEQUENCE_CONFLICT":409,"EVIDENCE_INVALID":422,"IDEMPOTENCY_CONFLICT":409,"INTERNAL_ERROR":500,"LEASE_EXPIRED":409,"LEASE_STALE":409,"LOCK_UNAVAILABLE":503,"MODEL_OUTPUT_INVALID":422,"PROVIDER_UNAVAILABLE":503,"REPOSITORY_NOT_AUTHORIZED":403,"REQUEST_INVALID":400,"RESOURCE_NOT_FOUND":404,"SOURCE_REF_NOT_FOUND":422,"SOURCE_TOO_LARGE":422,"SOURCE_UNSAFE":422,"STATE_CONFLICT":409,"TERMINAL_ALREADY_COMMITTED":409,"VERSION_CONFLICT":409,"WORKER_NOT_ATTESTED":403};
export const SCHEMA = SCHEMA_VALUE;

function tn(value) {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return 'boolean';
  if (typeof value === 'number') return Number.isInteger(value) ? 'integer' : 'number';
  if (typeof value === 'string') return 'string';
  if (Array.isArray(value)) return 'array';
  if (typeof value === 'object') return 'object';
  return 'unknown';
}

function has(obj, key) {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

function validate(node, instance, path, errors, root) {
  if (has(node, '$ref')) {
    const ref = node.$ref;
    if (!ref.startsWith('#/$defs/')) { errors.push([path, 'unsupported external $ref ' + ref]); return; }
    const target = ref.slice('#/$defs/'.length);
    if (!has(root.$defs, target)) { errors.push([path, 'unresolved $ref ' + ref]); return; }
    validate(root.$defs[target], instance, path, errors, root);
    return;
  }
  if (has(node, 'oneOf')) {
    let matches = 0;
    for (let i = 0; i < node.oneOf.length; i++) {
      const be = [];
      validate(node.oneOf[i], instance, path + '#oneOf[' + i + ']', be, root);
      if (be.length === 0) matches++;
    }
    if (matches !== 1) errors.push([path, 'oneOf requires exactly one match, got ' + matches]);
    return;
  }
  if (has(node, 'type')) {
    const actual = tn(instance);
    const want = node.type;
    const ok = want === 'number' ? (actual === 'integer' || actual === 'number') : actual === want;
    if (!ok) { errors.push([path, 'expected type ' + want + ', got ' + actual]); return; }
  }
  if (has(node, 'const') && instance !== node.const) {
    errors.push([path, 'expected const ' + JSON.stringify(node.const) + ', got ' + JSON.stringify(instance)]);
  }
  if (has(node, 'enum') && !node.enum.includes(instance)) {
    errors.push([path, 'value ' + JSON.stringify(instance) + ' not in enum ' + JSON.stringify(node.enum)]);
  }
  if (has(node, 'pattern') && typeof instance === 'string') {
    if (!new RegExp('^(?:' + node.pattern + ')$').test(instance)) {
      errors.push([path, 'value ' + JSON.stringify(instance) + ' does not match pattern ' + node.pattern]);
    }
  }
  if (has(node, 'minLength') && typeof instance === 'string' && instance.length < node.minLength) {
    errors.push([path, 'len ' + instance.length + ' < minLength ' + node.minLength]);
  }
  if (has(node, 'maxLength') && typeof instance === 'string' && instance.length > node.maxLength) {
    errors.push([path, 'len ' + instance.length + ' > maxLength ' + node.maxLength]);
  }
  if (has(node, 'minimum') && typeof instance === 'number' && instance < node.minimum) {
    errors.push([path, instance + ' < minimum ' + node.minimum]);
  }
  if (has(node, 'maximum') && typeof instance === 'number' && instance > node.maximum) {
    errors.push([path, instance + ' > maximum ' + node.maximum]);
  }
  if (instance !== null && typeof instance === 'object' && !Array.isArray(instance)) {
    if (has(node, 'required')) {
      for (const name of node.required) {
        if (!has(instance, name)) errors.push([path, 'missing required property ' + JSON.stringify(name)]);
      }
    }
    if (has(node, 'properties')) {
      for (const name of Object.keys(node.properties)) {
        if (has(instance, name)) validate(node.properties[name], instance[name], path + '.' + name, errors, root);
      }
      if (node.additionalProperties === false) {
        for (const key of Object.keys(instance)) {
          if (!has(node.properties, key)) errors.push([path, 'additional property ' + JSON.stringify(key) + ' is not permitted']);
        }
      }
    }
  }
  if (Array.isArray(instance) && has(node, 'items')) {
    if (has(node, 'minItems') && instance.length < node.minItems) errors.push([path, 'len ' + instance.length + ' < minItems ' + node.minItems]);
    if (has(node, 'maxItems') && instance.length > node.maxItems) errors.push([path, 'len ' + instance.length + ' > maxItems ' + node.maxItems]);
    for (let i = 0; i < instance.length; i++) validate(node.items, instance[i], path + '[' + i + ']', errors, root);
  }
}

export function validateDefinition(definition, instance) {
  if (!has(SCHEMA.$defs, definition)) return [['$defs', 'unknown definition ' + definition]];
  const errors = [];
  validate(SCHEMA.$defs[definition], instance, '$', errors, SCHEMA);
  return errors;
}

export function validateDocument(instance) {
  return validateDefinition('Document', instance);
}

export function classifyErrorCode(errors) {
  for (const entry of errors) {
    const p = entry[0];
    if (p === '$.evidence.path' || p.startsWith('$.evidence.path.') || p.startsWith('$.evidence.path#')) {
      return 'EVIDENCE_INVALID';
    }
  }
  return 'REQUEST_INVALID';
}
