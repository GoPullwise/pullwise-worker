Review the repository snapshot represented by the immutable attempt context
below. Apply the loaded review skill and reference method. Inspect enough of the
repository to report confirmed, actionable defects and honest coverage.

Attempt context (JSON):
{{ATTEMPT_CONTEXT_JSON}}

Return exactly one JSON object and no Markdown fence or commentary. The object
must contain exactly these keys:

- `summary`: a non-empty string;
- `findings`: an array of `pullwise-finding/v1` objects;
- `coverage`: an array of coverage entries from the Pullwise review contract.

Do not create the attempt envelope, usage, IDs, digests, artifacts, or terminal
state. The Worker owns those deterministic fields and validates this payload.
