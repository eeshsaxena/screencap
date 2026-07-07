// IntelligenceHelper — the on-device (Apple Foundation Models) segmentation
// helper invoked as a subprocess by the Python `OnDeviceProvider`
// (`src/screencap/segmentation/providers/ondevice.py`, U5 / KTD2).
//
// Contract (must match the Python side's IPC envelope):
//   • stdin  — the privacy-STRIPPED activity summary as JSON (the `summary`
//     sub-dict `build_activity_summary` produces; already ALLOW-only, U3/U4).
//   • stdout — exactly one JSON object:
//        {"status":"ok","result":{ tasks…, summary…, tags… }}
//        {"status":"unavailable","reason":"<why>"}
//     The `result` shape mirrors `segmentation/schema.py::_RESPONSE_SCHEMA`;
//     Python runs it through the shared `validate_llm_tasks` repair/reject net,
//     so this helper does not need to clamp/validate timestamps itself.
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
        // but an explicit envelope is cleaner).
        emitUnavailable("respond-failed")
    }
}

#endif

// MARK: - Entry point (top-level async — Swift 5.7+ in `main.swift`)

guard let summary = readSummary() else {
    emitUnavailable("no-input")
}

#if canImport(FoundationModels)
if #available(macOS 26.0, *) {
    await runOnDevice(summary: summary)
} else {
    emitUnavailable("os-below-macos-26")
}
#else
emitUnavailable("foundationmodels-unavailable")
#endif
