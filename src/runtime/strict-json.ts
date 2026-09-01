export class StrictJsonError extends SyntaxError {
  override readonly name = "StrictJsonError";
}

export function parseStrictJson(text: string): unknown {
  let index = 0;
  const fail = (message: string): never => {
    throw new StrictJsonError(`${message} at character ${index}`);
  };
  const whitespace = (): void => {
    while (/\s/u.test(text[index] ?? "")) index += 1;
  };
  const string = (): string => {
    const start = index;
    index += 1;
    while (index < text.length) {
      const code = text.charCodeAt(index);
      if (text[index] === '"') {
        index += 1;
        try {
          return JSON.parse(text.slice(start, index)) as string;
        } catch {
          return fail("malformed string");
        }
      }
      if (text[index] === "\\") {
        index += 1;
        if (text[index] === "u") index += 4;
      } else if (code < 0x20) {
        fail("control character in string");
      }
      index += 1;
    }
    return fail("unterminated string");
  };
  const value = (): unknown => {
    whitespace();
    if (text[index] === '"') return string();
    if (text[index] === "{") return object();
    if (text[index] === "[") return array();
    for (const [token, parsed] of [["true", true], ["false", false], ["null", null]] as const) {
      if (text.startsWith(token, index)) {
        index += token.length;
        return parsed;
      }
    }
    const match = text.slice(index).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/u);
    if (!match) return fail("malformed JSON");
    index += match[0].length;
    const parsed = Number(match[0]);
    if (!Number.isFinite(parsed)) return fail("non-finite number");
    return parsed;
  };
  const object = (): Record<string, unknown> => {
    index += 1;
    const result: Record<string, unknown> = {};
    const keys = new Set<string>();
    whitespace();
    if (text[index] === "}") {
      index += 1;
      return result;
    }
    while (index < text.length) {
      whitespace();
      if (text[index] !== '"') return fail("object key must be a string");
      const key = string();
      if (keys.has(key)) return fail(`duplicate object key ${JSON.stringify(key)}`);
      keys.add(key);
      whitespace();
      if (text[index] !== ":") return fail("missing colon");
      index += 1;
      result[key] = value();
      whitespace();
      if (text[index] === "}") {
        index += 1;
        return result;
      }
      if (text[index] !== ",") return fail("missing comma");
      index += 1;
    }
    return fail("unterminated object");
  };
  const array = (): unknown[] => {
    index += 1;
    const result: unknown[] = [];
    whitespace();
    if (text[index] === "]") {
      index += 1;
      return result;
    }
    while (index < text.length) {
      result.push(value());
      whitespace();
      if (text[index] === "]") {
        index += 1;
        return result;
      }
      if (text[index] !== ",") return fail("missing comma");
      index += 1;
    }
    return fail("unterminated array");
  };
  const result = value();
  whitespace();
  if (index !== text.length) fail("trailing data");
  return result;
}
