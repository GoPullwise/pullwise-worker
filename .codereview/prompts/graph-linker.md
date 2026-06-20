You are resolving cross-shard graph references.

Input:
- one unresolved reference
- source evidence
- a bounded candidate target list
- relevant import/export information
- relevant source excerpts

Task:
Determine whether the reference resolves to exactly one target.

Rules:
- Do not select a target only because names match.
- Confirm import path, namespace, receiver type, registry, framework configuration,
  or another concrete mechanism.
- If multiple targets remain possible, return ambiguous.
- If more context is required, return needs_context with exact file paths.
- Never invent a new target.

Output JSON only matching graph-link.schema.json.
