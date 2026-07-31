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
  RECORDER_FAILED,
  isOffscreenRequest,
  type OffscreenRequest,
  type OffscreenResponse,
} from "./protocol.js";
import { CaptureRecorder, browserRecorderDeps } from "./recorder.js";

const chunks = new ChunkStore(indexedDbChunkStorage());

const recorder = new CaptureRecorder(
  browserRecorderDeps(chunks, (error) => {
    // Unprompted: capture died on its own and the worker is not in the slice
    // path, so nothing else would tell it. A rejected send means the worker is
    // gone, which its own reconciliation already covers.
    void chrome.runtime.sendMessage({ type: RECORDER_FAILED, error }).catch(() => {});
  }),
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
