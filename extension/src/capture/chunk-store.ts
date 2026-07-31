/**
 * Where recorded bytes live between capture and upload.
 *
 * ## Why the slices are persisted as they arrive
 *
 * A `Blob` cannot be handed back to the service worker: extension messaging is
 * JSON-serialized by default, and structured-clone messaging needs a Chrome
 * version above this extension's floor. So the offscreen document keeps the
 * bytes itself and the worker gets a handle.
 *
 * Writing each slice on arrival rather than accumulating them buys the property
 * that matters more: a recording interrupted by a crash keeps everything up to
 * its last slice, instead of losing all of it.
 *
 * ## Why IndexedDB and not the origin private file system
 *
 * OPFS suits large binary media better on nearly every axis, but a writable
 * file stream commits to the real file only on close — a crash mid-recording
 * loses everything written since the stream opened, which is precisely the loss
 * this design exists to prevent. The durable OPFS path needs a synchronous
 * access handle, which is worker-only. IndexedDB commits per transaction, so a
 * slice is durable the moment its transaction completes. `chrome.storage` is
 * not an option at all: the offscreen document that writes here can reach only
 * `chrome.runtime`.
 *
 * Durability depends on the `unlimitedStorage` permission, which exempts this
 * data from quota eviction. Without it the browser may evict a recording that
 * has not been uploaded yet.
 *
 * The IndexedDB surface is injected so the store's own logic — ordering, gap
 * detection, session isolation — tests without a browser, matching the
 * `AllowlistDeps` split in `../permissions/allowlist.ts`.
 */

export interface ChunkRow {
  index: number;
  blob: Blob;
  byteLength: number;
}

export interface ChunkStorage {
  put(sessionId: string, row: ChunkRow): Promise<void>;
  /** All rows for a session, in no guaranteed order. */
  all(sessionId: string): Promise<ChunkRow[]>;
  removeSession(sessionId: string): Promise<void>;
}

export interface StoredRecording {
  /** Ordered by index. */
  chunks: Blob[];
  byteLength: number;
  /**
   * Whether the index sequence runs 0..n-1 with no hole.
   *
   * A recording that lost a slice in the middle is shorter than it should be
   * but looks perfectly valid as a list of blobs. Assembly needs to be able to
   * tell that apart from an honest recording, so the gap is reported rather
   * than silently closed.
   */
  complete: boolean;
}

const EMPTY: StoredRecording = { chunks: [], byteLength: 0, complete: true };

export class ChunkStore {
  constructor(private readonly storage: ChunkStorage) {}

  /**
   * Persist one slice.
   *
   * The index is the caller's, not generated here: the recorder knows the
   * order slices were produced in, and deriving it from a count would renumber
   * a retry into a duplicate.
   *
   * Failures propagate. The recorder must be able to fail the whole recording
   * on a bad write, and swallowing the error here would leave it recording into
   * a hole it never learns about.
   */
  async append(sessionId: string, index: number, blob: Blob): Promise<void> {
    await this.storage.put(sessionId, { index, blob, byteLength: blob.size });
  }

  async read(sessionId: string): Promise<StoredRecording> {
    const rows = await this.storage.all(sessionId);
    if (rows.length === 0) return EMPTY;

    const ordered = [...rows].sort((a, b) => a.index - b.index);
    return {
      chunks: ordered.map((row) => row.blob),
      byteLength: ordered.reduce((total, row) => total + row.byteLength, 0),
      complete: ordered.every((row, position) => row.index === position),
    };
  }

  /** Drop a session's bytes. Nothing calls this yet — upload and deletion are
   * later units — but it exists so they have a seam to call rather than a
   * reason to reach into IndexedDB themselves. */
  async remove(sessionId: string): Promise<void> {
    await this.storage.removeSession(sessionId);
  }
}

const DB_NAME = "screencap-capture";
const DB_VERSION = 1;
const STORE_NAME = "chunks";

/**
 * Adapter to real IndexedDB. Untested headlessly — the test environment is
 * Node, which has no IndexedDB — so it is kept as thin as the mapping allows
 * and verified by recording in a real browser.
 */
export function indexedDbChunkStorage(): ChunkStorage {
  return {
    put: (sessionId, row) =>
      withStore("readwrite", (store) => {
        store.put({ sessionId, ...row });
      }),

    all: async (sessionId) => {
      let rows: ChunkRow[] = [];
      await withStore("readonly", (store) => {
        // Indices are non-negative integers, so this range covers a session's
        // whole span without relying on how arrays sort against numbers.
        const request = store.getAll(
          IDBKeyRange.bound([sessionId, 0], [sessionId, Number.MAX_SAFE_INTEGER]),
        );
        request.onsuccess = () => {
          rows = request.result as ChunkRow[];
        };
      });
      return rows;
    },

    removeSession: (sessionId) =>
      withStore("readwrite", (store) => {
        store.delete(
          IDBKeyRange.bound([sessionId, 0], [sessionId, Number.MAX_SAFE_INTEGER]),
        );
      }),
  };
}

/**
 * Run one transaction and resolve when it *commits*.
 *
 * Resolving on the request's success would report durability the database has
 * not yet promised; `oncomplete` is the point at which the slice survives a
 * crash, which is the whole reason for writing slice-by-slice.
 */
function withStore(
  mode: IDBTransactionMode,
  run: (store: IDBObjectStore) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open(DB_NAME, DB_VERSION);

    open.onupgradeneeded = () => {
      const db = open.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: ["sessionId", "index"] });
      }
    };

    open.onerror = () => reject(open.error ?? new Error("Could not open the recording store"));

    open.onsuccess = () => {
      const db = open.result;
      try {
        const transaction = db.transaction(STORE_NAME, mode);
        run(transaction.objectStore(STORE_NAME));
        transaction.oncomplete = () => {
          db.close();
          resolve();
        };
        transaction.onabort = transaction.onerror = () => {
          db.close();
          reject(transaction.error ?? new Error("The recording store rejected a write"));
        };
      } catch (error) {
        db.close();
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    };
  });
}
