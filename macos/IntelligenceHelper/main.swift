// IntelligenceHelper — the on-device (Apple Foundation Models) segmentation
// helper invoked as a subprocess by the Python `OnDeviceProvider`
// (`src/screencap/segmentation/providers/ondevice.py`, U5 / KTD2).
//
// Contract (must match the Python side's IPC envelope):
//   • stdin  — one JSON object. A top-level `"task"` key selects the path
//     (absent / "segment" → segmentation; "answer" → free-form recall-answer,
//     SCR-243; "arbitrate" / "name-window" / "day-summary" → the window-scoped
//     verbs, SCR-275). For segmentation the object IS the privacy-STRIPPED
//     activity summary (`build_activity_summary`'s `summary` sub-dict;
//     ALLOW-only). For the answer path it is
//     `{"task":"answer","prompt":…,"evidence":…}` where `evidence` is already
//     privacy-stripped text. The windowed verbs take
//     `{"task":"arbitrate","windows":[{"i":0,"line":…},…]}`,
//     `{"task":"name-window","digest":{…}}`, and
//     `{"task":"day-summary","tasks":[{"name":…,"category":…,"minutes":…},…]}`
//     — every payload string privacy-stripped by Python before it reaches
//     stdin.
//   • stdout — exactly one JSON object:
//        {"status":"ok","result":{ tasks…, summary…, tags… }}   (segmentation)
//        {"status":"ok","result":"<answer text>"}                (recall-answer)
//        {"status":"ok","result":{"merges":[[…],…]}}             (arbitrate)
//        {"status":"ok","result":{"name":…,"category":…}}        (name-window)
//        {"status":"ok","result":{"overview":…,"tags":[…]}}      (day-summary)
//        {"status":"unavailable","reason":"<why>"}
//     The segmentation `result` shape mirrors
//     `segmentation/schema.py::_RESPONSE_SCHEMA`; Python runs it through the
//     shared `validate_llm_tasks` repair/reject net, so this helper does not
//     need to clamp/validate timestamps itself.
//     Unavailable reasons are semantic (KTD-3): `model-unavailable-*` for the
//     availability gate; `context-window` / `guardrail` / `refusal` /
//     `rate-limited` / `decoding-failure` / `unsupported` / `respond-failed`
//     for a failed respond call on any guided verb; `bad-request` for a
//     malformed windowed-verb payload (missing fields / wrong types — never a
//     crash).
//   • exit 0 in both the ok and unavailable cases (Python routes on the
//     envelope `status`, not the exit code). A non-zero exit / crash / hang is
//     ALSO treated as unavailable by Python — so any unexpected failure here
//     still degrades safely rather than blocking terminal_stage.
//
// Foundation Models is macOS 26+. This whole model path is `#available`-guarded;
// below the floor (or when Apple Intelligence is off / the model isn't ready)
// the helper emits an `unavailable` envelope and the Python side maps it to the
// distinct PROVIDER_UNAVAILABLE sentinel that U7's degradation ladder routes on.
//
// Real inference requires Apple Intelligence enabled at runtime (out of scope
// for CI — covered by the manual macOS-26 eval). This file is written to COMPILE
// against the macOS 26.4 SDK; the API surface below was confirmed against the
// FoundationModels `.swiftinterface` in Xcode 26.4, not guessed.

import Foundation

#if canImport(FoundationModels)
import FoundationModels
#endif

// MARK: - IPC envelope helpers

/// Emit an `unavailable` envelope and exit 0. Python maps this to the distinct
/// "could not run" sentinel.
func emitUnavailable(_ reason: String) -> Never {
    let obj = ["status": "unavailable", "reason": reason]
    if let data = try? JSONSerialization.data(withJSONObject: obj),
       let str = String(data: data, encoding: .utf8) {
        print(str)
    } else {
        print("{\"status\":\"unavailable\",\"reason\":\"encode-failed\"}")
    }
    exit(0)
}

/// Emit an `ok` envelope wrapping the model `result` and exit 0.
func emitOK(result: [String: Any]) -> Never {
    let obj: [String: Any] = ["status": "ok", "result": result]
    if let data = try? JSONSerialization.data(withJSONObject: obj),
       let str = String(data: data, encoding: .utf8) {
        print(str)
        exit(0)
    }
    emitUnavailable("result-encode-failed")
}

/// Emit an `ok` envelope whose `result` is a free-form answer STRING (the
/// recall-answer path), and exit 0. Python's `_parse_text_envelope` expects a
/// string `result` here, distinct from the segmentation dict `result`.
func emitOKText(_ text: String) -> Never {
    let obj: [String: Any] = ["status": "ok", "result": text]
    if let data = try? JSONSerialization.data(withJSONObject: obj),
       let str = String(data: data, encoding: .utf8) {
        print(str)
        exit(0)
    }
    emitUnavailable("result-encode-failed")
}

/// Read all of stdin and decode the activity-summary JSON object.
func readSummary() -> [String: Any]? {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard !data.isEmpty,
          let obj = try? JSONSerialization.jsonObject(with: data),
          let dict = obj as? [String: Any]
    else { return nil }
    return dict
}

/// Render the summary dict back to a compact JSON string to feed the model
/// prompt. The Python side already stripped and shaped it; we pass it through.
func summaryJSONString(_ summary: [String: Any]) -> String {
    guard let data = try? JSONSerialization.data(withJSONObject: summary),
          let str = String(data: data, encoding: .utf8)
    else { return "{}" }
    return str
}

// The prompt mirrors the Gemini backend's instructions so on-device output is
// shaped the same way the shared validator expects.
let promptInstructions = """
You are a productivity analyst examining a computer activity timeline from a \
screen recording. Identify the distinct TASKS the user performed — coherent \
units of work named by INTENT, not by app name. Brief app switches (< 30s) \
mid-task are absorbed. Cover every second with exactly one task (no gaps, no \
overlaps); the first task starts at 0:00:00; each task is at least 1 minute. \
For each task give start_time and end_time as relative H:MM:SS timestamps \
matching the timeline, a 2-3 word intent name, a 3-5 sentence description, a \
category, the apps involved, and a confidence. Also give a session summary \
(overview, primary_focus, time_breakdown, key_accomplishments) and 3-8 \
lowercase-hyphenated tags for the whole session.
"""

// Grounding instructions for the free-form recall-answer path (SCR-243). This
// MUST mirror the Python-side `_GROUNDING_INSTRUCTIONS` in
// `segmentation/generation_finish.py` (KTD3) — the two copies drive on-device
// and cloud answers with the same "answer only from the evidence" framing, and
// there is no shared validator to mask drift, so keep them in sync.
let answerInstructions = """
You are answering a question about the user's own recorded computer activity, \
using ONLY the evidence provided. Ground every claim in that evidence. If the \
evidence does not contain enough to answer, say so plainly and briefly — do not \
guess, invent, or draw on outside knowledge. Keep the answer concise.
"""

// Short verb-specific instructions for the window-scoped verbs (SCR-275,
// KTD-2/KTD-9): each spawn is one fresh session + one respond call, so each
// instruction covers exactly one bounded decision.

let arbitrateInstructions = """
You are reviewing candidate activity windows from a screen recording, listed \
one per line as "index: digest". Adjacent windows that belong to the same \
unit of work should be merged. Return groups of contiguous window indices to \
merge, each group in ascending order; return no groups if every boundary is a \
genuine task switch.
"""

let nameWindowInstructions = """
You are naming one window of computer activity from a screen recording. From \
the digest, give the task a 2-3 word name capturing the user's intent (not \
the app name) and a category: development, communication, research, admin, \
creative, or other.
"""

let daySummaryInstructions = """
You are summarizing a day of computer activity. Given the tasks the user \
performed with their categories and durations, write a 1-2 sentence overview \
of the session and 3-8 lowercase-hyphenated tags.
"""

// MARK: - Foundation Models path (macOS 26+, guided generation)

#if canImport(FoundationModels)

/// The task schema mirrored as a `@Generable` type so the model emits it via
/// guided generation. Field names match `_RESPONSE_SCHEMA` so the Python
/// validator consumes the JSON directly.
@available(macOS 26.0, *)
@Generable
struct GenTask {
    @Guide(description: "Relative start time as H:MM:SS, e.g. 0:02:00")
    var startTime: String
    @Guide(description: "Relative end time as H:MM:SS")
    var endTime: String
    @Guide(description: "2-3 word task name capturing intent, not the app")
    var name: String
    @Guide(description: "3-5 sentence description of what was worked on")
    var description: String
    @Guide(description: "One of: development, communication, research, admin, creative, other")
    var category: String
    @Guide(description: "Apps involved in this task")
    var appsUsed: [String]
    @Guide(description: "One of: high, medium, low")
    var confidence: String
}

@available(macOS 26.0, *)
@Generable
struct GenSummary {
    @Guide(description: "4-6 sentence overview of what the user accomplished")
    var overview: String
    @Guide(description: "The main category of work")
    var primaryFocus: String
    @Guide(description: "2-4 specific things completed")
    var keyAccomplishments: [String]
}

@available(macOS 26.0, *)
@Generable
struct GenSegmentation {
    @Guide(description: "The distinct tasks performed, covering the whole session")
    var tasks: [GenTask]
    var summary: GenSummary
    @Guide(description: "3-8 lowercase-hyphenated session tags")
    var tags: [String]
}

/// Convert the guided-generation result into the JSON-object shape the Python
/// validator (`_RESPONSE_SCHEMA`) expects (snake_case keys).
@available(macOS 26.0, *)
func resultDict(from seg: GenSegmentation) -> [String: Any] {
    let tasks: [[String: Any]] = seg.tasks.map { t in
        [
            "start_time": t.startTime,
            "end_time": t.endTime,
            "name": t.name,
            "description": t.description,
            "category": t.category,
            "apps_used": t.appsUsed,
            "confidence": t.confidence,
        ]
    }
    let summary: [String: Any] = [
        "overview": seg.summary.overview,
        "primary_focus": seg.summary.primaryFocus,
        "time_breakdown": [String: Any](),  // validator fills if empty
        "key_accomplishments": seg.summary.keyAccomplishments,
    ]
    return ["tasks": tasks, "summary": summary, "tags": seg.tags]
}

/// Map a failed respond call to the KTD-3 semantic reason taxonomy. The
/// categories are chosen to map from `LanguageModelSession.GenerationError`
/// (macOS 26) and to survive its OS-27 successor: anything unrecognized
/// degrades to the generic `respond-failed`. Applied to every guided verb,
/// including the legacy `segment` path (envelope shape unchanged — only the
/// reason string is more specific).
@available(macOS 26.0, *)
func unavailableReason(for error: Error) -> String {
    guard let generationError = error as? LanguageModelSession.GenerationError else {
        return "respond-failed"
    }
    switch generationError {
    case .exceededContextWindowSize:
        return "context-window"
    case .guardrailViolation:
        return "guardrail"
    case .refusal:
        return "refusal"
    case .rateLimited:
        return "rate-limited"
    case .decodingFailure:
        return "decoding-failure"
    case .unsupportedGuide, .unsupportedLanguageOrLocale:
        return "unsupported"
    default:
        // concurrentRequests, assetsUnavailable, and any future cases.
        return "respond-failed"
    }
}

@available(macOS 26.0, *)
func runOnDevice(summary: [String: Any]) async -> Never {
    // Runtime availability: Apple Intelligence must be enabled and the model
    // ready. Anything else → unavailable (Python degrades).
    let model = SystemLanguageModel.default
    switch model.availability {
    case .available:
        break
    case .unavailable(let reason):
        emitUnavailable("model-unavailable-\(reason)")
    }

    let session = LanguageModelSession(instructions: promptInstructions)
    let prompt = "ACTIVITY LOG:\n" + summaryJSONString(summary)

    do {
        let response = try await session.respond(
            to: prompt,
            generating: GenSegmentation.self,
            options: GenerationOptions(temperature: 0.1)
        )
        emitOK(result: resultDict(from: response.content))
    } catch {
        // A model error mid-generation degrades to unavailable rather than
        // crashing the helper (Python would treat a crash as unavailable too,
        // but an explicit envelope is cleaner). The reason carries the KTD-3
        // semantic taxonomy so Python's retry policy can route on it.
        emitUnavailable(unavailableReason(for: error))
    }
}

/// The free-form recall-answer path (SCR-243, U3): answer `prompt` grounded in
/// `evidence` and emit a STRING `result`. Unlike `runOnDevice`, this uses NO
/// guided generation (`@Generable`) — `respond(to:)` returns plain text, so a
/// grounded refusal is just a normal string. The Python `sanitize_answer`
/// hardens the returned text on the far side of the envelope.
@available(macOS 26.0, *)
func runAnswer(prompt: String, evidence: String) async -> Never {
    let model = SystemLanguageModel.default
    switch model.availability {
    case .available:
        break
    case .unavailable(let reason):
        emitUnavailable("model-unavailable-\(reason)")
    }

    let session = LanguageModelSession(instructions: answerInstructions)
    let modelInput = "EVIDENCE:\n" + evidence + "\n\nQUESTION:\n" + prompt

    do {
        let response = try await session.respond(
            to: modelInput,
            options: GenerationOptions(temperature: 0.2)
        )
        emitOKText(response.content)
    } catch {
        emitUnavailable("answer-failed")
    }
}

// MARK: - Window-scoped verbs (SCR-275, U2): arbitrate / name-window / day-summary
//
// Slim @Generable schemas (KTD-9) with a per-verb `maximumResponseTokens` cap.
// The cap is a runaway guard, not a formatting tool — a genuinely truncated
// generation surfaces as `decoding-failure` and Python's retry taxonomy
// (KTD-3) handles it.

/// Merge decisions over the heuristic candidate windows: groups of contiguous
/// window indices that belong to one task. Python validates contiguity and
/// range; an empty list keeps every heuristic boundary.
@available(macOS 26.0, *)
@Generable
struct GenMerges {
    @Guide(description: "Groups of contiguous window indices to merge into one task; empty when every boundary is a real task switch", .maximumCount(24))
    var merges: [[Int]]
}

@available(macOS 26.0, *)
@Generable
struct GenWindowName {
    @Guide(description: "2-3 word task name capturing intent, not the app")
    var name: String
    @Guide(description: "One of: development, communication, research, admin, creative, other")
    var category: String
}

@available(macOS 26.0, *)
@Generable
struct GenDaySummary {
    @Guide(description: "1-2 sentence overview of what the user accomplished")
    var overview: String
    @Guide(description: "3-8 lowercase-hyphenated session tags", .maximumCount(8))
    var tags: [String]
}

/// Gate on runtime model availability, exactly like the legacy verbs do.
@available(macOS 26.0, *)
func requireAvailableModel() {
    switch SystemLanguageModel.default.availability {
    case .available:
        break
    case .unavailable(let reason):
        emitUnavailable("model-unavailable-\(reason)")
    }
}

/// One-shot guided respond (KTD-2): fresh session, one call, semantic-reason
/// failure mapping. Shared by the three window-scoped verbs.
@available(macOS 26.0, *)
func respondGuided<Content: Generable>(
    instructions: String,
    prompt: String,
    generating type: Content.Type,
    maximumResponseTokens: Int
) async -> Content {
    requireAvailableModel()
    let session = LanguageModelSession(instructions: instructions)
    do {
        let response = try await session.respond(
            to: prompt,
            generating: type,
            options: GenerationOptions(
                temperature: 0.1,
                maximumResponseTokens: maximumResponseTokens
            )
        )
        return response.content
    } catch {
        emitUnavailable(unavailableReason(for: error))
    }
}

/// `{"task":"arbitrate","windows":[{"i":0,"line":"…"},…]}` → which contiguous
/// candidate windows to merge. Result: `{"merges":[[…],…]}`.
@available(macOS 26.0, *)
func runArbitrate(request: [String: Any]) async -> Never {
    guard let rawWindows = request["windows"] as? [[String: Any]], !rawWindows.isEmpty else {
        emitUnavailable("bad-request")
    }
    var lines: [String] = []
    for window in rawWindows {
        guard let index = window["i"] as? Int, let line = window["line"] as? String else {
            emitUnavailable("bad-request")
        }
        lines.append("\(index): \(line)")
    }
    let decision = await respondGuided(
        instructions: arbitrateInstructions,
        prompt: "CANDIDATE WINDOWS:\n" + lines.joined(separator: "\n"),
        generating: GenMerges.self,
        maximumResponseTokens: 512
    )
    emitOK(result: ["merges": decision.merges])
}

/// `{"task":"name-window","digest":{…}}` → a 2-3 word intent name plus a
/// category for one window. Result: `{"name":"…","category":"…"}`.
@available(macOS 26.0, *)
func runNameWindow(request: [String: Any]) async -> Never {
    guard let digest = request["digest"] as? [String: Any] else {
        emitUnavailable("bad-request")
    }
    let named = await respondGuided(
        instructions: nameWindowInstructions,
        prompt: "WINDOW DIGEST:\n" + summaryJSONString(digest),
        generating: GenWindowName.self,
        maximumResponseTokens: 256
    )
    emitOK(result: ["name": named.name, "category": named.category])
}

/// `{"task":"day-summary","tasks":[{"name":"…","category":"…","minutes":N},…]}`
/// → a short session overview + tags. Result: `{"overview":"…","tags":[…]}`.
@available(macOS 26.0, *)
func runDaySummary(request: [String: Any]) async -> Never {
    guard let rawTasks = request["tasks"] as? [[String: Any]], !rawTasks.isEmpty else {
        emitUnavailable("bad-request")
    }
    var lines: [String] = []
    for entry in rawTasks {
        guard let name = entry["name"] as? String,
              let category = entry["category"] as? String,
              let minutes = entry["minutes"] as? Double
        else {
            emitUnavailable("bad-request")
        }
        lines.append("- \(name) [\(category)] \(Int(minutes)) min")
    }
    let summary = await respondGuided(
        instructions: daySummaryInstructions,
        prompt: "TASKS PERFORMED:\n" + lines.joined(separator: "\n"),
        generating: GenDaySummary.self,
        maximumResponseTokens: 512
    )
    emitOK(result: ["overview": summary.overview, "tags": summary.tags])
}

#endif

// MARK: - Entry point (top-level async — Swift 5.7+ in `main.swift`)

guard let request = readSummary() else {
    emitUnavailable("no-input")
}

// Task discriminator (SCR-243, extended by SCR-275): absent / "segment" → the
// existing segmentation path (the bare summary dict is passed through
// unchanged); "answer" → the free-form recall-answer path; "arbitrate" /
// "name-window" / "day-summary" → the window-scoped verbs. Keeping the default
// as segment leaves the existing stdin shape byte-compatible.
let task = (request["task"] as? String) ?? "segment"

#if canImport(FoundationModels)
if #available(macOS 26.0, *) {
    switch task {
    case "answer":
        let prompt = request["prompt"] as? String ?? ""
        let evidence = request["evidence"] as? String ?? ""
        await runAnswer(prompt: prompt, evidence: evidence)
    case "arbitrate":
        await runArbitrate(request: request)
    case "name-window":
        await runNameWindow(request: request)
    case "day-summary":
        await runDaySummary(request: request)
    default:
        await runOnDevice(summary: request)
    }
} else {
    emitUnavailable("os-below-macos-26")
}
#else
emitUnavailable("foundationmodels-unavailable")
#endif
