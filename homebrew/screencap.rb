class Screencap < Formula
  include Language::Python::Virtualenv

  desc "macOS CLI for screen capture with privacy scrubbing"
  homepage "https://github.com/OpenAdaptAI/screencap"
  url "https://github.com/OpenAdaptAI/screencap/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "PLACEHOLDER_SHA256"
  license "MIT"

  depends_on "python@3.12"
  depends_on :macos

  def install
    venv = virtualenv_create(libexec, "python3.12")

    # Install vendored packages first
    venv.pip_install buildpath/"packages/openadapt-capture"
    venv.pip_install buildpath/"packages/openadapt-privacy[presidio]"

    # Install screencap itself
    venv.pip_install_and_link buildpath

    # Download spaCy model for privacy scrubbing
    system libexec/"bin/python", "-m", "spacy", "download", "en_core_web_trf"
  end

  def caveats
    <<~EOS
      ScreenCap requires macOS permissions to function:

      1. Screen Recording — System Settings > Privacy & Security > Screen Recording
         Grant access to your terminal app (Terminal, iTerm2, etc.)

      2. Accessibility — System Settings > Privacy & Security > Accessibility
         Grant access to your terminal app

      3. Microphone (for audio capture) — System Settings > Privacy & Security > Microphone
         Grant access to your terminal app

      Recordings are stored in ~/.screencap/recordings/ by default.
      Configure via ~/.screencap/config.toml or SCREENCAP_RECORDINGS_DIR env var.
    EOS
  end

  test do
    assert_match "screencap", shell_output("#{bin}/screencap --version")
  end
end
