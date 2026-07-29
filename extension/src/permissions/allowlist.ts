/**
 * The origin allow-list: which origins may ever be recorded.
 *
 * Two stores are involved and they answer different questions.
 *
 * - **Chrome's permission grants are the capture boundary.** {@link
 *   Allowlist.isAllowed} resolves against `chrome.permissions.contains` and
 *   never reads storage, so a stale stored entry can never authorize capture.
 * - **`chrome.storage.local` is the display layer.** It holds the ordered list
 *   the popup renders, and nothing more.
 *
 * They can drift, because `chrome.permissions.onRemoved` does **not** fire when
 * a user revokes site access from the `chrome://extensions` page — while
 * `contains()` reports that revoke correctly. An event-driven cache would
 * therefore go stale in precisely the case this feature must handle, so
 * {@link Allowlist.list} reconciles on every read instead of listening.
 *
 * Reconciliation runs in both directions. A stored entry with no live grant is
 * dropped; a live grant with no stored entry is adopted, because an origin that
 * can be recorded but cannot be seen is one the user cannot revoke. The
 * repairing write is best-effort — the *returned* list is already correct
 * whether or not the write lands.
 *
 * How each caller reaches {@link Allowlist.isAllowed} differs by context:
 * - The popup and the service worker construct an `Allowlist` and call it.
 * - **A content script cannot.** `chrome.permissions` is not exposed to content
 *   scripts, so a content script must ask the service worker over a message.
 *   Note that `isTrustedSender` in `../background/service-worker.ts` rejects any
 *   sender carrying a `tab` — which is every content script — so that verb needs
 *   its own sender predicate rather than reusing the auth one, which is
 *   deliberately stricter.
 */

import { isAdoptablePattern, normalizeOrigin, originLabel } from "./origins.js";

export const ALLOWLIST_STORAGE_KEY = "screencap.allowlist.origins";

export interface AllowlistStorage {
  get(key: string): Promise<string | null>;
  set(key: string, value: string): Promise<void>;
}

/**
 * Chrome's surface, injected so the store is testable without a browser and the
 * adapter below is the only untested mapping. Mirrors `AuthDeps` in
 * `../auth/firebase.ts`.
 */
export interface AllowlistDeps {
  permissionsRequest: (origins: string[]) => Promise<boolean>;
  permissionsRemove: (origins: string[]) => Promise<boolean>;
  permissionsContains: (origins: string[]) => Promise<boolean>;
  /** Granted host patterns only — the caller unwraps Chrome's `Permissions`. */
  permissionsGetAll: () => Promise<string[]>;
  storage: AllowlistStorage;
}

export interface AllowlistEntry {
  pattern: string;
  label: string;
}

export type AddResult =
  | { ok: true; entry: AllowlistEntry; portDropped: string | null }
  /** `invalid`: not expressible as an origin. `declined`: the user said no to
   * Chrome's own prompt. Both are ordinary outcomes, not errors — a transport
   * failure throws instead, mirroring the `NotSignedInError` /
   * `AuthUnavailableError` split in `../auth/firebase.ts`. */
  | { ok: false; reason: "invalid" | "declined" };

function toEntry(pattern: string): AllowlistEntry {
  return { pattern, label: originLabel(pattern) };
}

export class Allowlist {
  constructor(private readonly deps: AllowlistDeps) {}

  /**
   * Whether this URL may be recorded right now.
   *
   * Fail-closed on every uncertain path: an unparseable URL, a scheme outside
   * the origin model, or a permission check that throws all answer `false`.
   * Refusing to record costs a user one re-grant; recording an origin that was
   * never allowed cannot be walked back. `src/screencap/auth.py` takes the same
   * position where an unresolvable identity refuses the action.
   *
   * Pattern subsumption is Chrome's to decide — a broader grant such as
   * `https://*.example.com/*` covers this page, and `contains()` says so
   * without this module comparing strings itself.
   */
  async isAllowed(url: string): Promise<boolean> {
    const origin = normalizeOrigin(url);
    if (origin === null) return false;
    try {
      return await this.deps.permissionsContains([origin.pattern]);
    } catch {
      return false;
    }
  }

  /** The allow-list as it actually stands, reconciled against Chrome. */
  async list(): Promise<AllowlistEntry[]> {
    const reconciled = await this.reconcile();
    return reconciled.map(toEntry);
  }

  /**
   * Add an origin.
   *
   * The grant is requested before anything is stored, so a declined prompt
   * cannot leave an entry behind. The caller must invoke this from inside a
   * user gesture in an extension page: Chrome requires it, and delegating the
   * request to the service worker over a message throws.
   */
  async add(input: string): Promise<AddResult> {
    const origin = normalizeOrigin(input);
    if (origin === null) return { ok: false, reason: "invalid" };

    const granted = await this.deps.permissionsRequest([origin.pattern]);
    if (!granted) return { ok: false, reason: "declined" };

    const stored = await this.readStored();
    if (!stored.includes(origin.pattern)) {
      await this.persist([...stored, origin.pattern]);
    }
    return {
      ok: true,
      entry: toEntry(origin.pattern),
      portDropped: origin.portDropped,
    };
  }

  /**
   * Remove an origin, revoking the grant first.
   *
   * If the revoke does not take, the entry stays: a list that still shows a
   * live grant is honest, one that hides it is not.
   */
  async remove(pattern: string): Promise<void> {
    const revoked = await this.deps.permissionsRemove([pattern]);
    if (!revoked) {
      throw new Error(`Chrome did not revoke access to ${originLabel(pattern)}`);
    }
    const stored = await this.readStored();
    await this.persist(stored.filter((p) => p !== pattern));
  }

  /**
   * Resolve the stored list against Chrome's grants in both directions.
   *
   * Returns the corrected list first and persists it second, so a storage
   * failure degrades to a stale store rather than a wrong answer.
   */
  private async reconcile(): Promise<string[]> {
    const [stored, granted] = await Promise.all([
      this.readStored(),
      this.readGranted(),
    ]);

    const adoptable = granted.filter(isAdoptablePattern);
    const live = new Set(adoptable);

    // Deduplicated as it goes. Two popup windows reconciling at once, or an
    // `add` that raced another context's write, can leave the same pattern
    // stored twice; showing it twice would offer two Remove buttons for one
    // grant.
    const seen = new Set<string>();
    const reconciled: string[] = [];
    const take = (pattern: string): void => {
      if (seen.has(pattern)) return;
      seen.add(pattern);
      reconciled.push(pattern);
    };

    // Stored entries that still hold a grant keep their original order...
    for (const pattern of stored) if (live.has(pattern)) take(pattern);
    // ...then grants nothing is tracking yet are appended.
    for (const pattern of adoptable) take(pattern);

    if (!sameOrder(reconciled, stored)) {
      await this.persist(reconciled);
    }
    return reconciled;
  }

  /** Corrupt or absent storage reads as an empty list rather than throwing —
   * storage never authorizes capture, so an empty read costs display only. */
  private async readStored(): Promise<string[]> {
    let raw: string | null;
    try {
      raw = await this.deps.storage.get(ALLOWLIST_STORAGE_KEY);
    } catch {
      return [];
    }
    if (raw === null) return [];
    try {
      const parsed: unknown = JSON.parse(raw);
      return Array.isArray(parsed)
        ? parsed.filter((p): p is string => typeof p === "string")
        : [];
    } catch {
      return [];
    }
  }

  /** Grant state that cannot be read means nothing can be shown as allowed. */
  private async readGranted(): Promise<string[]> {
    try {
      return await this.deps.permissionsGetAll();
    } catch {
      return [];
    }
  }

  /** Best-effort. A failed repair leaves the store stale; the caller already
   * holds the corrected answer. */
  private async persist(patterns: string[]): Promise<void> {
    try {
      await this.deps.storage.set(ALLOWLIST_STORAGE_KEY, JSON.stringify(patterns));
    } catch {
      // Intentionally swallowed — see the method docstring.
    }
  }
}

function sameOrder(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((value, i) => value === b[i]);
}

/** Adapter to the real Chrome APIs. Every other test injects fakes, so this
 * mapping is covered by `chrome-deps.test.ts`. */
export function chromeAllowlistDeps(): AllowlistDeps {
  return {
    permissionsRequest: (origins) => chrome.permissions.request({ origins }),
    permissionsRemove: (origins) => chrome.permissions.remove({ origins }),
    permissionsContains: (origins) => chrome.permissions.contains({ origins }),
    permissionsGetAll: async () => (await chrome.permissions.getAll()).origins ?? [],
    storage: {
      get: async (key) => (await chrome.storage.local.get(key))[key] ?? null,
      set: async (key, value) => chrome.storage.local.set({ [key]: value }),
    },
  };
}
