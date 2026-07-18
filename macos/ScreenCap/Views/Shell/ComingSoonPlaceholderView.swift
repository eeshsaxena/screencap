import SwiftUI

/// An honest placeholder for a primary route whose surface hasn't landed yet
/// (Tasks → U6, Clips → U11). Stage A of the day-first restructure exposes all
/// four R1 sidebar rows before Tasks and Clips exist, so clicking either lands
/// on this "coming in this update" surface — never a broken or blank route.
struct ComingSoonPlaceholderView: View {
    let title: String
    let message: String

    var body: some View {
        VStack(spacing: SCMetrics.space4) {
            ShellLogoMark(size: 40)
            Text(title)
                .font(SCTypography.serifDayHeading)
                .foregroundStyle(Color.scInk)
            Text("Coming in this update")
                .font(SCTypography.mono(size: 11))
                .foregroundStyle(Color.scInkMuted)
            Text(message)
                .font(SCTypography.bodyText)
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 360)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(SCMetrics.space8)
        .background(Color.scPaper)
    }
}
