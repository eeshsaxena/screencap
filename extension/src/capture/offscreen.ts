/**
 * The offscreen document's entry point.
 *
 * Owns the one {@link CaptureRecorder} for the extension — Chrome permits only
 * one offscreen document at a time, so one recorder per document is one
 * recorder overall.
 *
 * This context can reach only `chrome.runtime`; there is no `chrome.storage`
 * and no `chrome.tabCapture` here. That is why the session record is the
 * worker's to write and the recorded bytes are this document's to keep.
 */

import { isExtensionPageSender } from "../background/senders.js";
import { ChunkStore, indexedDbChunkStorage } from "./chunk-store.js";
import {
  RECORDER_ENDED,
  RECORDER_FAILED,
  isOffscreenRequest,
  type OffscreenRequest,
  type OffscreenResponse,
} from "./protocol.js";
import { CaptureRecorder, browserRecorderDeps } from "./recorder.js";

const chunks = new ChunkStore(indexedDbChunkStorage());

/**
 * Tell the worker something happened that it did not ask for.
 *
 * If the message cannot be delivered, this document closes itself. That is not
 * a giving-up: its own disappearance is a signal the worker's reconciliation
 * already understands, so the session degrades to `interrupted` — bytes
 * stranded, honestly reported — instead of staying `recording` forever behind a
 * recorder that is already dead.
 */
function announce(message: { type: string; [key: string]: unknown }): void {
  void chrome.runtime.sendMessage(message).catch(() => window.close());
}

const recorder = new CaptureRecorder(
  browserRecorderDeps(
    chunks,
    (error) => announce({ type: RECORDER_FAILED, error }),
    (sessionId) => announce({ type: RECORDER_ENDED, sessionId }),
  ),
);

async function handle(request: OffscreenRequest): Promise<OffscreenResponse> {
  switch (request.type) {
    case "offscreen.start":
      return {
        type: "offscreen.start",
        result: await recorder.start(request.sessionId, request.source),
      };
    case "offscreen.pause":
      return { type: "offscreen.pause", changed: recorder.pause() };
    case "offscreen.resume":
      return { type: "offscreen.resume", changed: recorder.resume() };
    case "offscreen.stop":
      return { type: "offscreen.stop", summary: await recorder.stop() };
  }
}

chrome.runtime.onMessage.addListener(
  (request: unknown, sender, sendResponse: (r: OffscreenResponse) => void) => {
    if (!isOffscreenRequest(request)) return false;
    if (!isExtensionPageSender(sender)) {
      sendResponse({ type: "offscreen.error", error: "Unauthorized sender" });
      return false;
    }
    handle(request)
      .then(sendResponse)
      .catch((error: unknown) => {
        sendResponse({
          type: "offscreen.error",
          error: error instanceof Error ? error.message : String(error),
        });
      });
    // Keeps the message channel open for the async response above.
    return true;
  },
);
