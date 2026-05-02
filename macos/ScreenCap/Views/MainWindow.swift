import SwiftUI

/// Top-level window content. Sidebar (Calendar / Recordings / Privacy) +
/// detail area. Calendar is the default. Calendar day click filters the
/// recordings list to that day; "Show all" clears the filter. Privacy is a
/// stub until Unit 18.
///
/// Unit 13 will overlay a recording banner on the detail area.
struct MainWindow: View {
    @EnvironmentObject private var recorder: RecorderController
    @EnvironmentObject private var permissions: PermissionController
    @EnvironmentObject private var index: RecordingsIndex

    enum SidebarSection: Hashable { case calendar, recordings, privacy }

    @State private var section: SidebarSection = .calendar
    @State private var selectedDate: Date?
    @State private var visibleMonth: Date = startOfCurrentMonth()
    @State private var showingPermissionsSheet = false

    var body: some View {
        NavigationSplitView {
            sidebar
        } detail: {
            detail
        }
        .sheet(isPresented: $showingPermissionsSheet) {
            FirstRunPermissionsView(isPresented: $showingPermissionsSheet)
                .environmentObject(permissions)
                .environmentObject(recorder)
        }
        .onAppear {
            // The sheet owns its own poll lifecycle (see FirstRunPermissionsView)
            // so MainWindow only triggers the initial visibility check here.
            if !permissions.allRequiredGranted {
                showingPermissionsSheet = true
            }
        }
        .onChange(of: section) { new in
            if new != .recordings { selectedDate = nil }
        }
    }

    private var sidebar: some View {
        List(selection: $section) {
            NavigationLink(value: SidebarSection.calendar) {
                Label("Calendar", systemImage: "calendar")
            }
            NavigationLink(value: SidebarSection.recordings) {
                Label("Recordings", systemImage: "list.bullet.rectangle")
            }
            NavigationLink(value: SidebarSection.privacy) {
                Label("Privacy", systemImage: "lock.shield")
            }
            .disabled(true)
        }
        .listStyle(.sidebar)
        .frame(minWidth: 180)
        .navigationTitle("ScreenCap")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    Task { await index.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh recordings")
            }
        }
    }

    @ViewBuilder
    private var detail: some View {
        switch section {
        case .calendar:
            CalendarView(
                selectedDate: $selectedDate,
                visibleMonth: $visibleMonth
            ) { day in
                selectedDate = day
                section = .recordings
            }
        case .recordings:
            RecordingsListView(filterDay: $selectedDate) { day in
                selectedDate = nil
                visibleMonth = day
                section = .calendar
            }
        case .privacy:
            VStack {
                Text("Privacy")
                    .font(.title2.bold())
                Text("Coming in Unit 18.")
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private static func startOfCurrentMonth() -> Date {
        let comps = Calendar.current.dateComponents([.year, .month], from: Date())
        return Calendar.current.date(from: comps) ?? Date()
    }
}
