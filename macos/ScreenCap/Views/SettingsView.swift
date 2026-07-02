import SwiftUI

/// The account & cloud settings surface (U8, R15): plan status, the upload
/// destination toggle, the training-contribution toggle, an entry into the shared
/// cloud-setup flow (R3/R4), and sign-out (gated on no active upload). Reads plan
/// status from `/v0/auth.entitlements` via `CloudAuthController`; writes go
/// through the U5 config setters.
struct SettingsView: View {
    @ObservedObject var auth: CloudAuthController
    @ObservedObject var uploads: UploadCoordinator

    @State private var destination = "local"
    @State private var trainingOptIn = false
    @State private var loaded = false
    @State private var showingSetup = false

    private let destinations = ["local", "cloud", "both"]

    var body: some View {
        Form {
            Section("Account & Cloud") {
                planStatusRow
                accountRow
            }

            Section("Recording destination") {
                Picker("Upload recordings to", selection: $destination) {
                    Text("This Mac only").tag("local")
                    Text("Cloud").tag("cloud")
                    Text("Both").tag("both")
                }
                .onChange(of: destination) { newValue in
                    guard loaded else { return }  // ignore the initial load assignment
                    Task { _ = await auth.setUploadDestination(newValue) }
                }
                Toggle("Contribute scrubbed data to training", isOn: $trainingOptIn)
                    .onChange(of: trainingOptIn) { newValue in
                        guard loaded else { return }
                        Task { _ = await auth.setTrainingContribution(newValue) }
                    }
                Text("Training contribution uses only scrubbed, masked data and is revocable at any time.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .frame(width: 460, height: 360)
        .task { await load() }
        .sheet(isPresented: $showingSetup) {
            CloudSetupView(
                auth: auth,
                onComplete: { didGrant in
                    showingSetup = false
                    Task {
                        // A cloud user's destination follows an actual grant, not
                        // merely opening the sheet on an already-entitled account.
                        if didGrant {
                            _ = await auth.setUploadDestination("cloud")
                            destination = "cloud"
                        }
                        await auth.refreshEntitlements()
                    }
                },
                onDismiss: { showingSetup = false }
            )
        }
    }

    @ViewBuilder
    private var planStatusRow: some View {
        HStack {
            Text("Plan")
            Spacer()
            switch auth.entitlementStatus {
            case .unknown:
                ProgressView().controlSize(.small)
            case .active(let plan):
                Text(plan.capitalized).foregroundStyle(Color.scSuccessFg)
            case .free:
                Text("Free (local only)").foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private var accountRow: some View {
        if auth.status.isSignedIn {
            HStack {
                Text(auth.status.accountLabel.map { "Signed in as \($0)" } ?? "Signed in")
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Sign Out") { Task { await auth.signOut() } }
                    // Never sign out mid-upload (an in-flight signed-URL request
                    // would hit NotSignedIn). Gates on the app-wide upload count.
                    .disabled(!(auth.isSignedIn && !uploads.isUploadInFlight))
            }
            if !auth.entitlementStatus.isActive {
                Button("Set up cloud") { showingSetup = true }
            }
        } else {
            HStack {
                Text("Not signed in").foregroundStyle(.secondary)
                Spacer()
                Button("Set up cloud") { showingSetup = true }
            }
        }
    }

    private func load() async {
        await auth.refreshEntitlements()
        let settings = await auth.fetchCloudSettings()
        destination = settings.destination == "ask" ? "local" : settings.destination
        trainingOptIn = settings.trainingContribution
        loaded = true
    }
}
