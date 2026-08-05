type JsonSchema = Record<string, unknown> | boolean;

interface ValidationContext {
  root: Record<string, unknown>;
  external: Readonly<Record<string, Record<string, unknown>>>;
}

export function validatesGeneratedSchema(
  schema: Record<string, unknown>,
  value: unknown,
  external: Readonly<Record<string, Record<string, unknown>>> = {},
): boolean {
  return validate(schema, value, { root: schema, external });
}

function validate(schema: JsonSchema, value: unknown, context: ValidationContext): boolean {
  if (schema === true) return true;
  if (schema === false) return false;
  if (typeof schema.$ref === "string") {
    const resolved = resolveReference(schema.$ref, context);
    return resolved !== null && validate(resolved.schema, value, resolved.context);
  }

  if (Array.isArray(schema.allOf) && !schema.allOf.every((item) => validate(asSchema(item), value, context))) {
    return false;
  }
  if (Array.isArray(schema.anyOf) && !schema.anyOf.some((item) => validate(asSchema(item), value, context))) {
    return false;
  }
  if (Array.isArray(schema.oneOf)) {
    const matches = schema.oneOf.filter((item) => validate(asSchema(item), value, context)).length;
    if (matches !== 1) return false;
  }
  if (schema.not !== undefined && validate(asSchema(schema.not), value, context)) return false;
  if (schema.if !== undefined && validate(asSchema(schema.if), value, context)) {
    if (schema.then !== undefined && !validate(asSchema(schema.then), value, context)) return false;
  } else if (schema.else !== undefined && !validate(asSchema(schema.else), value, context)) {
    return false;
  }

  if (schema.const !== undefined && !deepEqual(value, schema.const)) return false;
  if (Array.isArray(schema.enum) && !schema.enum.some((item) => deepEqual(value, item))) return false;
  if (schema.type !== undefined && !matchesType(value, schema.type)) return false;

  if (typeof value === "string") {
    const length = Array.from(value).length;
    if (typeof schema.minLength === "number" && length < schema.minLength) return false;
    if (typeof schema.maxLength === "number" && length > schema.maxLength) return false;
    if (typeof schema.pattern === "string" && !(new RegExp(schema.pattern).test(value))) return false;
  }
  if (typeof value === "number") {
    if (typeof schema.minimum === "number" && value < schema.minimum) return false;
    if (typeof schema.maximum === "number" && value > schema.maximum) return false;
  }
  if (Array.isArray(value)) {
    if (typeof schema.minItems === "number" && value.length < schema.minItems) return false;
    if (typeof schema.maxItems === "number" && value.length > schema.maxItems) return false;
    if (schema.uniqueItems === true) {
      const canonical = value.map(canonicalJson);
      if (new Set(canonical).size !== canonical.length) return false;
    }
    if (schema.items !== undefined && !value.every((item) => validate(asSchema(schema.items), item, context))) {
      return false;
    }
  }
  if (isRecord(value)) {
    const keys = Object.keys(value);
    if (typeof schema.maxProperties === "number" && keys.length > schema.maxProperties) return false;
    const required = Array.isArray(schema.required)
      ? schema.required.filter((key): key is string => typeof key === "string")
      : [];
    if (!required.every((key) => key in value)) return false;
    const properties = isRecord(schema.properties) ? schema.properties : {};
    for (const [key, propertyValue] of Object.entries(value)) {
      const propertySchema = properties[key];
      if (propertySchema !== undefined) {
        if (!validate(asSchema(propertySchema), propertyValue, context)) return false;
      } else if (schema.additionalProperties === false) {
        return false;
      } else if (isRecord(schema.additionalProperties) || typeof schema.additionalProperties === "boolean") {
        if (!validate(schema.additionalProperties as JsonSchema, propertyValue, context)) return false;
      }
    }
  }
  return true;
}

function resolveReference(
  reference: string,
  context: ValidationContext,
): { schema: JsonSchema; context: ValidationContext } | null {
  if (reference.startsWith("#/")) {
    const value = resolvePointer(context.root, reference.slice(1));
    return value === undefined ? null : { schema: asSchema(value), context };
  }
  const [documentId, fragment = ""] = reference.split("#", 2);
  const externalRoot = context.external[documentId];
  if (externalRoot === undefined) return null;
  const value = fragment.length === 0 ? externalRoot : resolvePointer(externalRoot, fragment);
  return value === undefined
    ? null
    : { schema: asSchema(value), context: { root: externalRoot, external: context.external } };
}

function resolvePointer(root: unknown, pointer: string): unknown {
  if (pointer === "") return root;
  if (!pointer.startsWith("/")) return undefined;
  return pointer.slice(1).split("/").reduce<unknown>((current, rawPart) => {
    if (!isRecord(current)) return undefined;
    const part = rawPart.replaceAll("~1", "/").replaceAll("~0", "~");
    return current[part];
  }, root);
}

function matchesType(value: unknown, expected: unknown): boolean {
  const types = Array.isArray(expected) ? expected : [expected];
  return types.some((type) => {
    if (type === "null") return value === null;
    if (type === "object") return isRecord(value);
    if (type === "array") return Array.isArray(value);
    if (type === "integer") return Number.isSafeInteger(value);
    if (type === "number") return typeof value === "number" && Number.isFinite(value);
    return typeof value === type;
  });
}

function deepEqual(left: unknown, right: unknown): boolean {
  return canonicalJson(left) === canonicalJson(right);
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function asSchema(value: unknown): JsonSchema {
  return typeof value === "boolean" || isRecord(value) ? value : false;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
