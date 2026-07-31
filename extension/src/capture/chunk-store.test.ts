import { describe, expect, it } from "vitest";

import { ChunkStore, type ChunkRow, type ChunkStorage } from "./chunk-store.js";

/**
 * In-memory stand-in for the IndexedDB primitive. Deliberately returns rows in
 * insertion order rather than key order — `getAll` over a compound key range is
 * not contractually sorted the way the store needs, so ordering must be the
 * store's own job and the fake must not paper over that.
 */
function fakeStorage() {
  const rows: { sessionId: string; row: ChunkRow }[] = [];
  let failNextPut: Error | null = null;

  const storage: ChunkStorage = {
    put: async (sessionId, row) => {
      if (failNextPut) {
        const error = failNextPut;
        failNextPut = null;
        throw error;
      }
      rows.push({ sessionId, row });
    },
    all: async (sessionId) =>
      rows.filter((r) => r.sessionId === sessionId).map((r) => r.row),
    removeSession: async (sessionId) => {
      for (let i = rows.length - 1; i >= 0; i -= 1) {
        if (rows[i]!.sessionId === sessionId) rows.splice(i, 1);
      }
    },
  };

  return {
    storage,
    failNextPutWith: (error: Error) => void (failNextPut = error),
    rowCount: () => rows.length,
  };
}

function slice(text: string): Blob {
  return new Blob([text], { type: "video/webm" });
}

async function textOf(chunks: Blob[]): Promise<string[]> {
  return Promise.all(chunks.map((chunk) => chunk.text()));
}

describe("append and read", () => {
  it("reads chunks back in order with nothing lost", async () => {
    const store = new ChunkStore(fakeStorage().storage);
    await store.append("s1", 0, slice("a"));
    await store.append("s1", 1, slice("b"));
    await store.append("s1", 2, slice("c"));

    const recording = await store.read("s1");

    expect(await textOf(recording.chunks)).toEqual(["a", "b", "c"]);
    expect(recording.complete).toBe(true);
    expect(recording.byteLength).toBe(3);
  });

  it("orders by index even when slices land out of order", async () => {
    // The store cannot assume the primitive hands rows back sorted.
    const store = new ChunkStore(fakeStorage().storage);
    await store.append("s1", 2, slice("c"));
    await store.append("s1", 0, slice("a"));
    await store.append("s1", 1, slice("b"));

    expect(await textOf((await store.read("s1")).chunks)).toEqual(["a", "b", "c"]);
  });

  it("reports a hole as incomplete rather than as a shorter recording", async () => {
    // A failed append in the middle leaves a gap. Reading that back as a valid
    // two-chunk recording would ship a video silently missing its middle.
    const store = new ChunkStore(fakeStorage().storage);
    await store.append("s1", 0, slice("a"));
    await store.append("s1", 2, slice("c"));

    const recording = await store.read("s1");

    expect(recording.complete).toBe(false);
    expect(recording.chunks).toHaveLength(2);
  });

  it("keeps two sessions' slices apart", async () => {
    // An interrupted recording's chunks stay on disk while a new one is
    // written, so sessions coexist even though they never run concurrently.
    const store = new ChunkStore(fakeStorage().storage);
    await store.append("s1", 0, slice("one"));
    await store.append("s2", 0, slice("two"));

    expect(await textOf((await store.read("s1")).chunks)).toEqual(["one"]);
    expect(await textOf((await store.read("s2")).chunks)).toEqual(["two"]);
  });

  it("reads an unknown session as empty rather than throwing", async () => {
    const recording = await new ChunkStore(fakeStorage().storage).read("never-written");

    expect(recording.chunks).toEqual([]);
    expect(recording.byteLength).toBe(0);
    // Nothing written is not the same as something written badly — an empty
    // recording is trivially whole.
    expect(recording.complete).toBe(true);
  });
});

describe("append failure", () => {
  it("propagates the failure instead of resolving successfully", async () => {
    // The recorder's job is to fail the recording loudly, and it cannot do that
    // if the store swallows the error and lets capture continue with a hole.
    const fake = fakeStorage();
    const store = new ChunkStore(fake.storage);
    fake.failNextPutWith(new Error("quota exceeded"));

    await expect(store.append("s1", 0, slice("a"))).rejects.toThrow("quota exceeded");
    expect(fake.rowCount()).toBe(0);
  });
});

describe("remove", () => {
  it("drops one session's chunks and leaves the other's intact", async () => {
    const store = new ChunkStore(fakeStorage().storage);
    await store.append("s1", 0, slice("one"));
    await store.append("s2", 0, slice("two"));

    await store.remove("s1");

    expect((await store.read("s1")).chunks).toEqual([]);
    expect(await textOf((await store.read("s2")).chunks)).toEqual(["two"]);
  });
});
