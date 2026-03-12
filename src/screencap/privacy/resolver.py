"""Priority-based detection span resolver.

Replaces the naive _merge_detections() that blindly unioned overlapping spans.
Resolution rules:
1. Exact dedup: same (start, end, entity_type) -> keep highest-priority source
2. Identical span, different type: keep higher-priority source's type
3. Nested spans: KEEP BOTH if types are compatible (e.g., EMAIL inside CONNECTION_STRING)
4. Partial overlap, unrelated types: keep BOTH as separate detections (DO NOT union)
5. Partial overlap, same source: union into wider span (same behavior as before)
"""

from __future__ import annotations

from screencap.privacy import Detection

# Source priority — higher number = higher priority
SOURCE_PRIORITY: dict[str, int] = {
    "secrets": 40,
    "regex": 30,
    "pii-gliner": 20,
    "pii-presidio": 10,
}

# Entity types that can nest (inner type, outer type)
COMPATIBLE_NESTING: set[tuple[str, str]] = {
    ("EMAIL", "CONNECTION_STRING"),
    ("PASSWORD", "CONNECTION_STRING"),
    ("API_KEY", "CONNECTION_STRING"),
}


class DetectionResolver:
    """Resolve overlapping detections by source priority."""

    def __init__(
        self,
        source_priority: dict[str, int] | None = None,
        compatible_nesting: set[tuple[str, str]] | None = None,
    ) -> None:
        self._priority = source_priority or SOURCE_PRIORITY
        self._nesting = compatible_nesting or COMPATIBLE_NESTING

    def _get_priority(self, det: Detection) -> int:
        return self._priority.get(det.source, 0)

    def _is_compatible_nesting(self, inner: Detection, outer: Detection) -> bool:
        """Check if inner nested inside outer is a compatible pair."""
        pair = (inner.entity_type, outer.entity_type)
        return pair in self._nesting

    def resolve(self, detections: list[Detection]) -> list[Detection]:
        """Resolve overlapping detections. Returns non-overlapping results.

        Algorithm:
        1. Sort by (start, -end) so wider spans come first at same start
        2. Exact-span dedup: same (start, end) -> keep higher priority source
        3. Walk sorted detections, resolve overlaps:
           - Nested + compatible types -> keep both
           - Nested + incompatible -> keep higher priority
           - Partial overlap from different sources -> keep both (no union!)
           - Partial overlap from same source -> union
        """
        if not detections:
            return []

        # Sort by (start, -end) — wider spans first when same start
        sorted_dets = sorted(detections, key=lambda d: (d.start, -d.end))

        # Phase 1: Exact-span dedup — same (start, end) -> keep higher priority
        deduped: list[Detection] = []
        for det in sorted_dets:
            if deduped and deduped[-1].start == det.start and deduped[-1].end == det.end:
                prev = deduped[-1]
                prev_pri = self._get_priority(prev)
                det_pri = self._get_priority(det)
                if det_pri > prev_pri or (det_pri == prev_pri and det.score > prev.score):
                    deduped[-1] = det
            else:
                deduped.append(det)

        # Phase 2: Union overlapping spans from the SAME source.
        # This must happen before cross-source resolution so that a
        # different-source "keep both" break can't shadow a same-source
        # overlap with an earlier entry.
        by_source: dict[str, list[Detection]] = {}
        for det in deduped:
            by_source.setdefault(det.source, []).append(det)

        same_source_merged: list[Detection] = []
        for source_dets in by_source.values():
            merged: list[Detection] = [source_dets[0]]
            for det in source_dets[1:]:
                prev = merged[-1]
                if det.start < prev.end:
                    # Check for compatible nesting before unioning
                    is_nested = det.start >= prev.start and det.end <= prev.end
                    is_prev_nested = prev.start >= det.start and prev.end <= det.end
                    if is_nested and self._is_compatible_nesting(det, prev):
                        merged.append(det)
                        continue
                    if is_prev_nested and self._is_compatible_nesting(prev, det):
                        merged.append(det)
                        continue
                    # Overlapping within same source — union
                    new_start = min(prev.start, det.start)
                    new_end = max(prev.end, det.end)
                    winner = det if det.score > prev.score else prev
                    merged[-1] = Detection(
                        entity_type=winner.entity_type,
                        start=new_start,
                        end=new_end,
                        score=max(prev.score, det.score),
                        source=winner.source,
                    )
                else:
                    merged.append(det)
            same_source_merged.extend(merged)

        # Re-sort after same-source merging
        same_source_merged.sort(key=lambda d: (d.start, -d.end))

        # Phase 3: Resolve cross-source overlaps.
        result: list[Detection] = []
        for det in same_source_merged:
            merged_flag = False
            for i in range(len(result) - 1, -1, -1):
                prev = result[i]

                # No overlap — spans are sorted, so once we pass, we're done
                if det.start >= prev.end:
                    break

                # Overlap exists: det.start < prev.end
                is_nested = det.start >= prev.start and det.end <= prev.end

                if is_nested:
                    if self._is_compatible_nesting(det, prev):
                        break  # Keep both
                    # Incompatible nesting — keep higher priority
                    prev_pri = self._get_priority(prev)
                    det_pri = self._get_priority(det)
                    if det_pri > prev_pri:
                        result[i] = det
                    merged_flag = True
                    break

                # Check if prev is nested inside det (det is wider)
                is_prev_nested = prev.start >= det.start and prev.end <= det.end
                if is_prev_nested:
                    if self._is_compatible_nesting(prev, det):
                        break  # Keep both
                    prev_pri = self._get_priority(prev)
                    det_pri = self._get_priority(det)
                    if det_pri >= prev_pri:
                        result[i] = det
                    merged_flag = True
                    break

                # Partial overlap, different sources (same-source already resolved)
                # Keep BOTH — no union across sources
                break

            if not merged_flag:
                result.append(det)

        return sorted(result, key=lambda d: (d.start, d.end))
