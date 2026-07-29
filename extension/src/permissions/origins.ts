/**
 * The one place a string becomes a Chrome host-permission match pattern.
 *
 * Every pattern the extension hands to `chrome.permissions` comes from here, so
 * the rules about what an allow-list entry may cover are stated once instead of
 * being re-derived at each call site. Nothing in this module touches Chrome —
 * it is pure string vocabulary, and the allow-list store layers grant state on
 * top of it.
 *
 * The shape is `<scheme>://<host>/*`. The path is required by Chrome for a host
 * permission and ignored when matching, so `/*` is used by convention.
 */

/** Schemes an allow-list entry may use. `file:`, `chrome:`, `chrome-extension:`
 * and `data:` are outside the origin model R11 describes and would widen the
 * Web Store review surface for no benefit to the operator persona. */
const ALLOWED_PROTOCOLS = new Set(["http:", "https:"]);

/**
 * Matches a leading `scheme://` so a bare host can be told from a full URL.
 *
 * The `://` is required, not decoration. Without it this also matches the host
 * half of `localhost:3000` — `localhost:` is a syntactically valid scheme — so
 * a typed `host:port` would skip the https default, parse with protocol
 * `localhost:`, and be rejected as "not a site address". A local dev server on
 * a port is the operator persona's most likely input.
 */
const HAS_SCHEME = /^[a-z][a-z0-9+.-]*:\/\//i;

/** A well-formed host-permission pattern: http or https, some host, `/*`. */
const HOST_PATTERN = /^(https?):\/\/([^/]+)\/\*$/;

export interface NormalizedOrigin {
  /** What gets passed to `chrome.permissions.request`. */
  pattern: string;
  /** What the user is shown. Always describes {@link pattern}, never the raw
   * input — see {@link portDropped}. */
  label: string;
  /**
   * The port that was discarded, or `null` if there was none.
   *
   * Chrome silently ignores ports in host-permission match patterns, and a
   * pattern carrying one can end up granting nothing at all
   * (https://issues.chromium.org/issues/40517388). Dropping it is the only
   * shape that works — but `http://localhost:3000` then grants *every* port on
   * `localhost`, which is wider than what was asked for. Reporting the drop
   * lets the caller say so rather than widening the grant silently.
   */
  portDropped: string | null;
}

/**
 * Turn user input or a page URL into a match pattern, or `null` if it cannot be
 * expressed as one.
 *
 * Returns `null` rather than throwing: this runs on arbitrary typed text and on
 * every URL the browser visits, where `chrome://newtab` is an ordinary input and
 * not an error worth an exception.
 */
export function normalizeOrigin(input: string): NormalizedOrigin | null {
  const trimmed = input.trim();
  if (trimmed === "") return null;

  // A bare host is what someone types; assume https rather than rejecting it.
  const candidate = HAS_SCHEME.test(trimmed) ? trimmed : `https://${trimmed}`;

  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    return null;
  }

  if (!ALLOWED_PROTOCOLS.has(url.protocol)) return null;
  if (url.hostname === "") return null;
  // Wildcards are never *requested* — per KTD6 an entry grants one exact host.
  // Chrome may still widen a grant through its own prompt; that arrives via
  // getAll() and is handled by isAdoptablePattern, not here.
  if (url.hostname.includes("*")) return null;

  const label = `${url.protocol}//${url.hostname}`;
  return {
    pattern: `${label}/*`,
    label,
    // `url.port` is empty for a scheme's default port, so `:443` on https is
    // correctly reported as no drop at all.
    portDropped: url.port === "" ? null : url.port,
  };
}

/** Render a stored pattern for display. Handles patterns Chrome granted as well
 * as ones this module produced, so a widened `https://*.example.com/*` still
 * reads correctly in the list. */
export function originLabel(pattern: string): string {
  return pattern.endsWith("/*") ? pattern.slice(0, -2) : pattern;
}

/**
 * Whether a pattern found in Chrome's grant state may be adopted into the
 * allow-list.
 *
 * The manifest has to declare a broad optional envelope — every http and https
 * host — because allow-listed origins are chosen at runtime and cannot be
 * enumerated at build time. Chrome's documentation describes
 * `Permissions.origins` as including origins declared in the manifest's
 * permission keys, so it is not certain that `getAll()` excludes that
 * ungranted envelope. Adopting it would write "everything" into the allow-list
 * and read back as everything-allowed — a silent fail-open of the boundary this
 * feature exists to draw. Refusing a bare `*` host removes the dependence on
 * that ambiguity entirely.
 *
 * A subdomain wildcard (`https://*.example.com/*`) IS adoptable: it is a real,
 * bounded grant the user made through Chrome's own prompt, and hiding it from
 * the list would be the exact harm adoption prevents — an origin that can be
 * recorded but cannot be seen or revoked.
 */
export function isAdoptablePattern(pattern: string): boolean {
  const match = HOST_PATTERN.exec(pattern);
  if (!match) return false;
  const host = match[2];
  return host !== "*";
}

/**
 * Whether a pattern covers every host on its scheme — the all-sites envelope.
 *
 * Distinct from `!isAdoptablePattern(p)`, which is also true for a malformed
 * string. This is the specific shape that, if actually granted, makes every
 * origin recordable, so the caller can say so rather than rendering an empty
 * allow-list as "nothing can be recorded".
 */
export function isBroadHostPattern(pattern: string): boolean {
  const match = HOST_PATTERN.exec(pattern);
  return match !== null && match[2] === "*";
}

/**
 * A host that cannot have been granted on its own, used to ask Chrome whether
 * an all-sites grant is genuinely live.
 *
 * `.invalid` is reserved by RFC 2606 and can never be a real site, so nobody
 * can have allow-listed it. `permissions.contains` therefore answers true for
 * it only when some broader pattern subsumes it. This settles the ambiguity
 * `isAdoptablePattern` documents — Chrome's docs leave open whether
 * `getAll().origins` includes the manifest's declared-but-ungranted envelope,
 * and a declared-only envelope must not be reported to the user as live.
 */
export const BROAD_GRANT_PROBE = "https://broad-grant-probe.invalid/*";
