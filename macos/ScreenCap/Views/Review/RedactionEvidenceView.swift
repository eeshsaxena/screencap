import SwiftUI

/// Pure summary helpers, extracted so the protection tally and labels are
/// unit-tested without a SwiftUI render.
enum RedactionSummary {
    static func totalRedactions(_ r: ReviewRedaction?) -> Int {
        (r?.summary ?? [:]).values.reduce(0, +)
    }

    static func hiddenSegmentCount(_ r: ReviewRedaction?) -> Int {
        r?.blockedIntervals?.count ?? 0
    }

    static func hasFailClosed(_ r: ReviewRedaction?) -> Bool {
        !(r?.failClosed?.isEmpty ?? true)
    }

    /// Nothing was redacted, hidden, or fail-closed — the calm state.
    static func isEmpty(_ r: ReviewRedaction?) -> Bool {
        totalRedactions(r) == 0 && hiddenSegmentCount(r) == 0 && !hasFailClosed(r)
    }

    /// Friendly, operator-facing label for an entity type.
    static func friendlyName(_ entityType: String) -> String {
        switch entityType {
        case "EMAIL_ADDRESS": return "Email"
        case "PERSON": return "Name"
        case "PHONE_NUMBER": return "Phone number"
        case "CREDIT_CARD": return "Credit card"
        case "US_SSN", "SSN": return "SSN"
        case "IP_ADDRESS": return "IP address"
        case "URL": return "URL"
        case "LOCATION": return "Location"
        case "IBAN_CODE": return "Bank account"
        case "CRYPTO": return "Crypto address"
        default:
            return entityType.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    static func pluralize(_ noun: String, _ count: Int) -> String {
        guard count != 1 else { return noun }
        // Suffix-aware: words ending in s/x/z/ch/sh take "es" (so "IP address"
        // → "IP addresses", not "IP addresss").
        let lower = noun.lowercased()
        for suffix in ["s", "x", "z", "ch", "sh"] where lower.hasSuffix(suffix) {
            return "\(noun)es"
        }
        return "\(noun)s"
    }

    /// One-line protection headline framed as protection ("removed"/"hid"), or
    /// nil when nothing entity/segment-level was redacted (the fail-closed
    /// callout is surfaced separately, so it does not drive this headline).
    static func headline(_ r: ReviewRedaction?) -> String? {
        let total = totalRedactions(r)
        let hidden = hiddenSegmentCount(r)
        guard total > 0 || hidden > 0 else { return nil }
        var parts: [String] = []
        if total > 0 {
            parts.append("removed \(total) sensitive \(pluralize("item", total))")
        }
        if hidden > 0 {
            parts.append("hid \(hidden) \(pluralize("segment", hidden))")
        }
        return "Protected before upload — " + parts.joined(separator: ", ") + "."
    }

    /// Per-category breakdown, sorted by descending count then name, for the
    /// summary view's chips.
    static func breakdown(_ r: ReviewRedaction?) -> [(label: String, count: Int)] {
        (r?.summary ?? [:])
            .sorted { ($0.value, $1.key) > ($1.value, $0.key) }
            .map { (friendlyName($0.key), $0.value) }
    }
}

/// Per-recording redaction summary (R8), framed as protection. Shows the entity
/// breakdown + hidden-segment count, or a calm "nothing required redaction"
/// state. The fail-closed callout is a *separate* view (see `FailClosedCallout`)
/// so the "couldn't analyze" signal never blends into the protection tally.
struct RedactionEvidenceView: View {
    let redaction: ReviewRedaction?

    var body: some View {
        if let headline = RedactionSummary.headline(redaction) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Image(systemName: "lock.shield.fill").foregroundStyle(.green)
                    Text(headline).font(.callout.weight(.medium))
                    Spacer()
                }
                let chips = RedactionSummary.breakdown(redaction)
                if !chips.isEmpty {
                    HStack(spacing: 6) {
                        ForEach(chips, id: \.label) { chip in
                            Text("\(chip.count) \(RedactionSummary.pluralize(chip.label, chip.count))")
                                .font(.caption2)
                                .padding(.horizontal, 6).padding(.vertical, 2)
                                .background(.green.opacity(0.12))
                                .clipShape(Capsule())
                        }
                        Spacer()
                    }
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 8)
        } else if !RedactionSummary.hasFailClosed(redaction) {
            HStack(spacing: 6) {
                Image(systemName: "checkmark.seal").foregroundStyle(.secondary)
                Text("Nothing required redaction.")
                    .font(.callout).foregroundStyle(.secondary)
                Spacer()
            }
            .padding(.horizontal, 12).padding(.vertical, 8)
        }
    }
}

/// The distinct fail-closed signal (R14): an amber callout, visually separate
/// from both the entity summary and the per-moment markers, shown when the
/// scrubber couldn't analyze some content and removed it to be safe.
struct FailClosedCallout: View {
    let redaction: ReviewRedaction?

    var body: some View {
        if RedactionSummary.hasFailClosed(redaction) {
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.shield.fill")
                    .foregroundStyle(.orange)
                Text("Some content couldn't be analyzed and was removed to be safe.")
                    .font(.caption)
                Spacer()
            }
            .padding(.horizontal, 12).padding(.vertical, 6)
            .background(.orange.opacity(0.12))
        }
    }
}

/// Non-blocking timing advisory (SCR-107): the recording's timing metadata
/// couldn't be read — a corrupt, truncated, or otherwise unreadable
/// `recording.db`. The review stays playable (the video renders), so this is a
/// degraded-not-failed signal: an amber callout explaining why the timeline is
/// absent, distinct from the `ok: false` failure path. Renders nothing when
/// timing read cleanly (including a benign event-free recording).
struct TimingUnavailableCallout: View {
    let timingError: Bool

    var body: some View {
        if timingError {
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                Text("Timeline unavailable — this recording's metadata couldn't be read.")
                    .font(.caption)
                Spacer()
            }
            .padding(.horizontal, 12).padding(.vertical, 6)
            .background(.orange.opacity(0.12))
        }
    }
}

/// Persistent (not collapsed) coverage disclosure (R9), rendered from the
/// envelope's structured `coverage` facts. The allowed-app on-screen-PII blind
/// spot is emphasized over the benign facts because it is the only one
/// requiring operator action. Framed as content-assurance, not access-assurance
/// (SCR-64).
struct CoverageStrip: View {
    let coverage: ReviewCoverage?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            // The one fact that needs the operator's eyes, emphasized first.
            if coverage?.allowedAppScreenshotPiiManualReview ?? true {
                Label {
                    Text("Check the frames above — on-screen text inside allowed apps isn't auto-removed.")
                        .font(.caption2)
                } icon: {
                    Image(systemName: "eye.trianglebadge.exclamationmark")
                        .foregroundStyle(.orange)
                }
            }
            HStack(spacing: 12) {
                benignFact(coverage?.videoLocalOnly ?? true, "Video stays on this Mac")
                benignFact(coverage?.audioLocalOnly ?? true, "Audio stays on this Mac")
                if coverage?.transcriptUploadedScrubbed ?? false {
                    benignFact(true, "Transcript uploads scrubbed")
                }
                Spacer()
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.06))
    }

    @ViewBuilder
    private func benignFact(_ show: Bool, _ text: String) -> some View {
        if show {
            Label {
                Text(text).font(.caption2).foregroundStyle(.secondary)
            } icon: {
                Image(systemName: "checkmark.circle").foregroundStyle(.secondary)
            }
        }
    }
}
