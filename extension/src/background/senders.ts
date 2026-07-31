/**
 * Who is allowed to drive the extension's privileged verbs.
 *
 * Shared by the service worker and the offscreen document because both accept
 * messages and both must refuse the same senders. It lives apart from either
 * listener so importing the predicate does not drag in that listener's
 * module-load side effects — the auth session, or `MediaRecorder`.
 */

/**
 * Only extension pages qualify.
 *
 * A `tab` on the sender means a content script, which runs on allow-listed
 * origins. A page must never be able to start or stop a recording, or end a
 * session, just because the user visited it.
 */
export function isExtensionPageSender(sender: chrome.runtime.MessageSender): boolean {
  return sender.id === chrome.runtime.id && sender.tab === undefined;
}
