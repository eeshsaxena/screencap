import Foundation

// U6 — the New-recording sheet's pure decision layer, factored out of the view
// so the open gate, the honesty-substituted header copy (KTD-9), the mic-meter
// gate, and the initial audio choice are unit-testable (NewRecordingSheetPolicyTests).

enum NewRecordingSheetPolicy {

    /// The sheet may open only when no recording is in flight (KTD-4; logic 780).
    /// A start / stop / active recording all suppress it — the Library header
    /// button becomes "Stop recording" instead (U6).
    static func canPresent(recorderState: RecordingState) -> Bool {
        !recorderState.isRecording
    }

    /// The sheet header caption, reflecting the real `upload_default` (KTD-9): it
    /// may say "stays on this Mac" only when uploads are not the default. `cloud`
    /// / `both` flip it to a truthful "uploads by your choice" so the copy is
    /// never falsely reassuring; `local` / `ask` / unknown keep "stays on this
    /// Mac" — those recordings do stay local until an explicit Review-window
    /// upload.
    static func headerCaption(uploadDefault: String?) -> String {
        switch uploadDefault {
        case "cloud", "both":
            return "uploads by your choice"
        default:
            return "stays on this Mac"
        }
    }

    /// The mic level meter runs only when microphone TCC is already granted —
    /// opening the sheet must never trigger a permission prompt (U6). The meter
    /// is a passive level read, not a request.
    static func shouldRunMeter(micGranted: Bool) -> Bool {
        micGranted
    }

    /// The mic toggle's initial value = the persisted `audio_default`, defaulting
    /// to on when the daemon omits it.
    static func initialAudioOn(audioDefault: Bool?) -> Bool {
        audioDefault ?? true
    }
}
