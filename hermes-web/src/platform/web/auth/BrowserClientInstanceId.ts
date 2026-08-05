const CLIENT_INSTANCE_STORAGE_KEY = "hermes.web.client_instance_id.v1";
const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** Stable local identity enables the bounded same-client crash/refresh lease grace. */
export function getOrCreateBrowserClientInstanceId(
  storage: Pick<Storage, "getItem" | "setItem"> = localStorage,
  createUuid: () => string = () => crypto.randomUUID(),
): string {
  try {
    const existing = storage.getItem(CLIENT_INSTANCE_STORAGE_KEY);
    if (existing !== null && CANONICAL_UUID.test(existing)) return existing;
  } catch {
    // Storage can be unavailable in privacy modes; an ephemeral UUID remains fail-safe.
  }

  const created = createUuid();
  if (!CANONICAL_UUID.test(created)) throw new Error("Browser client identity generator returned an invalid UUID");
  try {
    storage.setItem(CLIENT_INSTANCE_STORAGE_KEY, created);
  } catch {
    // Do not log or persist anywhere else.
  }
  return created;
}
