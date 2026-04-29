"""Tests for screencap.network.ca_lifecycle (V1).

Covers CA generation, filesystem hardening primitives, install/verify/
uninstall via the macOS ``security`` CLI (mocked at the subprocess
boundary), the SHA-256-primary / SHA-1-fallback uninstall policy, and
the missing-identity CN-based fallback.

V1 scope only -- crypto/keyring/KEK/DEK tests live in V1.5's
``test_crypto.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from screencap.network import ca_lifecycle
from screencap.network.ca_lifecycle import (
    CA_COMMON_NAME,
    CertIdentity,
    SecurityCommandError,
    chmod_600_or_raise,
    generate_ca,
    install_ca,
    is_expiring_soon,
    mkdir_700_or_raise,
    uninstall_ca,
    verify_ca,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    """Build a CompletedProcess for mock subprocess returns."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def confdir(tmp_path: Path) -> Path:
    """Mode-700 confdir used by the CA generation tests."""
    d = tmp_path / "proxy"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return d


@pytest.fixture(autouse=True)
def _redirect_default_confdir(monkeypatch, tmp_path):
    """Redirect ``DEFAULT_CONFDIR`` so the verify/uninstall paths read
    from a tmp dir, not the real ``~/.screencap/proxy/``.

    Tests that need a custom confdir override this with their own
    ``monkeypatch.setattr``.
    """
    fake = tmp_path / "default_confdir"
    fake.mkdir(mode=0o700)
    os.chmod(fake, 0o700)
    monkeypatch.setattr(ca_lifecycle, "DEFAULT_CONFDIR", fake)
    return fake


# ---------------------------------------------------------------------------
# Filesystem hardening
# ---------------------------------------------------------------------------


class TestChmod600OrRaise:
    def test_sets_mode_600_on_freshly_written_file(self, tmp_path: Path):
        f = tmp_path / "secret.pem"
        f.write_text("hello")
        # Default umask typically yields 0o644.
        os.chmod(f, 0o644)
        chmod_600_or_raise(f)
        assert (f.stat().st_mode & 0o777) == 0o600

    def test_idempotent_when_already_600(self, tmp_path: Path):
        f = tmp_path / "secret.pem"
        f.write_text("hello")
        os.chmod(f, 0o600)
        chmod_600_or_raise(f)  # must not raise
        assert (f.stat().st_mode & 0o777) == 0o600

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            chmod_600_or_raise(tmp_path / "ghost.pem")

    def test_foreign_owner_raises_with_owner_check(self, tmp_path: Path):
        f = tmp_path / "secret.pem"
        f.write_text("hello")
        real_stat = f.stat()

        # Build a fake stat result with a different uid.
        class FakeStat:
            st_uid = real_stat.st_uid + 12345
            st_mode = real_stat.st_mode
            st_size = real_stat.st_size

        with patch.object(Path, "stat", lambda self, **kw: FakeStat()):
            with pytest.raises(PermissionError) as exc_info:
                chmod_600_or_raise(f)
        msg = str(exc_info.value)
        assert "owned by uid" in msg
        assert str(FakeStat.st_uid) in msg
        assert "current user" in msg


class TestMkdir700OrRaise:
    def test_creates_with_mode_700(self, tmp_path: Path):
        d = tmp_path / "newdir"
        mkdir_700_or_raise(d)
        assert d.is_dir()
        assert (d.stat().st_mode & 0o777) == 0o700

    def test_idempotent_when_already_700(self, tmp_path: Path):
        d = tmp_path / "newdir"
        d.mkdir(mode=0o700)
        os.chmod(d, 0o700)
        mkdir_700_or_raise(d)  # must not raise
        assert (d.stat().st_mode & 0o777) == 0o700

    def test_chmods_existing_wider_dir(self, tmp_path: Path):
        d = tmp_path / "newdir"
        d.mkdir(mode=0o755)
        os.chmod(d, 0o755)
        mkdir_700_or_raise(d)
        assert (d.stat().st_mode & 0o777) == 0o700

    def test_creates_parents(self, tmp_path: Path):
        d = tmp_path / "a" / "b" / "c"
        mkdir_700_or_raise(d)
        assert d.is_dir()
        assert (d.stat().st_mode & 0o777) == 0o700


# ---------------------------------------------------------------------------
# CA generation
# ---------------------------------------------------------------------------


class TestGenerateCa:
    def test_writes_both_pem_files(self, confdir: Path):
        combined = generate_ca(confdir)
        cert_only = confdir / "mitmproxy-ca-cert.pem"
        assert combined == confdir / "mitmproxy-ca.pem"
        assert combined.exists()
        assert cert_only.exists()
        # Both chmod-600.
        assert (combined.stat().st_mode & 0o777) == 0o600
        assert (cert_only.stat().st_mode & 0o777) == 0o600
        # Combined PEM contains both PRIVATE KEY and CERTIFICATE blocks.
        text = combined.read_text()
        assert "-----BEGIN PRIVATE KEY-----" in text
        assert "-----BEGIN CERTIFICATE-----" in text

    def test_cert_parses_with_30_day_expiry(self, confdir: Path):
        from cryptography import x509

        combined = generate_ca(confdir, days=30)
        cert = x509.load_pem_x509_certificate(combined.read_bytes())
        not_after = cert.not_valid_after_utc
        now = datetime.now(timezone.utc)
        delta = not_after - now
        # 30 days +/- 1 day for clock skew + test runtime.
        assert timedelta(days=29) <= delta <= timedelta(days=31)
        # Subject CN matches.
        cn_attrs = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        assert cn_attrs[0].value == CA_COMMON_NAME
        # CA flag is set.
        bc_ext = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc_ext.value.ca is True
        # KeyUsage includes key_cert_sign + crl_sign.
        ku_ext = cert.extensions.get_extension_for_class(x509.KeyUsage)
        assert ku_ext.value.key_cert_sign is True
        assert ku_ext.value.crl_sign is True

    def test_readonly_confdir_raises_permission_error(self, confdir: Path):
        os.chmod(confdir, 0o500)  # r-x, not writable
        try:
            with pytest.raises(PermissionError) as exc_info:
                generate_ca(confdir)
            # Message must be actionable -- includes the confdir path
            # and a remediation hint.
            msg = str(exc_info.value)
            assert str(confdir) in msg
            assert "writable" in msg.lower()
        finally:
            os.chmod(confdir, 0o700)

    def test_missing_confdir_raises_filenotfound(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            generate_ca(tmp_path / "ghost-confdir")


# ---------------------------------------------------------------------------
# Expiry checking
# ---------------------------------------------------------------------------


class TestIsExpiringSoon:
    def test_returns_false_when_no_cert(self, _redirect_default_confdir):
        # Clean confdir, no PEM files.
        assert is_expiring_soon(threshold_days=7) is False

    def test_boundary_4_days_with_5_day_threshold_returns_true(self, _redirect_default_confdir):
        # generate_ca with 4-day expiry.
        generate_ca(_redirect_default_confdir, days=4)
        assert is_expiring_soon(threshold_days=5) is True

    def test_boundary_6_days_with_5_day_threshold_returns_false(self, _redirect_default_confdir):
        generate_ca(_redirect_default_confdir, days=6)
        assert is_expiring_soon(threshold_days=5) is False


# ---------------------------------------------------------------------------
# Install -- mocked subprocess
# ---------------------------------------------------------------------------


class TestInstallCa:
    def test_happy_path_persists_identity(self, confdir: Path):
        cert_path = generate_ca(confdir)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            identity = install_ca(cert_path)

        # CertIdentity returned with both hashes populated, uppercase.
        assert identity.cn == CA_COMMON_NAME
        assert len(identity.sha1_hex) == 40
        assert len(identity.sha256_hex) == 64
        assert identity.sha1_hex == identity.sha1_hex.upper()
        assert identity.sha256_hex == identity.sha256_hex.upper()

        # ca-identity.json persisted with mode 600.
        identity_json = confdir / "ca-identity.json"
        assert identity_json.exists()
        assert (identity_json.stat().st_mode & 0o777) == 0o600
        loaded = json.loads(identity_json.read_text())
        assert loaded["cn"] == identity.cn
        assert loaded["sha256_hex"] == identity.sha256_hex
        assert loaded["sha1_hex"] == identity.sha1_hex

    def test_invokes_security_with_correct_args(self, confdir: Path):
        cert_path = generate_ca(confdir)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            install_ca(cert_path)

        assert mock_run.call_count == 1
        argv = mock_run.call_args.args[0]
        assert argv[:5] == [
            "security",
            "add-trusted-cert",
            "-r",
            "trustRoot",
            "-k",
        ]
        # Last arg is the cert path.
        assert argv[-1] == str(cert_path)
        # Login keychain referenced.
        assert "login.keychain-db" in argv[5]

    def test_security_failure_raises_with_stderr(self, confdir: Path):
        cert_path = generate_ca(confdir)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed(
                1, stderr="security: SecKeychainItemImport: invalid"
            )
            with pytest.raises(SecurityCommandError) as exc_info:
                install_ca(cert_path)

        err = exc_info.value
        assert err.returncode == 1
        assert "invalid" in err.stderr
        assert "security" in str(err)
        # ca-identity.json NOT persisted on failure.
        assert not (confdir / "ca-identity.json").exists()

    def test_missing_cert_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            install_ca(tmp_path / "ghost.pem")


# ---------------------------------------------------------------------------
# Verify -- integration with mocked subprocess
# ---------------------------------------------------------------------------


def _find_certificate_stdout(sha1_hex: str) -> str:
    """Mock output for ``security find-certificate -c <CN> -a -Z``."""
    return (
        f"keychain: \"/Users/test/Library/Keychains/login.keychain-db\"\n"
        f"version: 256\n"
        f"class: 0x80001000\n"
        f'    "labl"<blob>="screencap proxy CA"\n'
        f"SHA-1 hash: {sha1_hex}\n"
    )


def _trust_settings_stdout(sha1_hex: str, *, with_trust_root: bool = True) -> str:
    """Mock output for ``security dump-trust-settings``."""
    result = "kSecTrustSettingsResultTrustRoot" if with_trust_root else "kSecTrustSettingsResultUnspecified"
    return (
        f"Cert 0: screencap proxy CA\n"
        f"   SHA-1 hash: {sha1_hex}\n"
        f"   Number of trust settings: 1\n"
        f"   Trust Setting 0:\n"
        f"      kSecTrustSettingsResult = {result}\n"
    )


class TestVerifyCa:
    def test_returns_false_when_no_pem(self, _redirect_default_confdir):
        # Empty confdir, no PEM.
        assert verify_ca() is False

    def test_returns_true_when_installed_and_trusted(
        self, _redirect_default_confdir, monkeypatch
    ):
        # Generate the cert so verify can recompute its SHA-1.
        cert_pem = generate_ca(_redirect_default_confdir)
        identity = ca_lifecycle._compute_identity(cert_pem.read_bytes())

        def fake_run(argv, *args, **kwargs):
            if argv[:2] == ["security", "find-certificate"]:
                return _completed(0, stdout=_find_certificate_stdout(identity.sha1_hex))
            if argv[:2] == ["security", "dump-trust-settings"]:
                # User domain returns the trust root.
                if "-d" not in argv:
                    return _completed(0, stdout=_trust_settings_stdout(identity.sha1_hex))
                # Admin domain has nothing.
                return _completed(1, stdout="")
            return _completed(0)

        with patch("subprocess.run", side_effect=fake_run):
            assert verify_ca() is True

    def test_returns_false_when_sha1_missing_in_keychain(
        self, _redirect_default_confdir
    ):
        generate_ca(_redirect_default_confdir)

        def fake_run(argv, *args, **kwargs):
            if argv[:2] == ["security", "find-certificate"]:
                # Wrong SHA-1 -- attacker-planted same-CN cert scenario.
                return _completed(0, stdout=_find_certificate_stdout("A" * 40))
            return _completed(0)

        with patch("subprocess.run", side_effect=fake_run):
            assert verify_ca() is False

    def test_returns_false_when_not_a_trust_root(
        self, _redirect_default_confdir
    ):
        cert_pem = generate_ca(_redirect_default_confdir)
        identity = ca_lifecycle._compute_identity(cert_pem.read_bytes())

        def fake_run(argv, *args, **kwargs):
            if argv[:2] == ["security", "find-certificate"]:
                return _completed(0, stdout=_find_certificate_stdout(identity.sha1_hex))
            if argv[:2] == ["security", "dump-trust-settings"]:
                # Cert is in keychain but kSecTrustSettingsResultUnspecified
                # -- not a trust root.
                return _completed(
                    0, stdout=_trust_settings_stdout(identity.sha1_hex, with_trust_root=False)
                )
            return _completed(0)

        with patch("subprocess.run", side_effect=fake_run):
            assert verify_ca() is False


# ---------------------------------------------------------------------------
# Uninstall -- SHA-256 primary, SHA-1 fallback, not-found = success
# ---------------------------------------------------------------------------


class TestUninstallCa:
    def test_sha256_attempted_first_succeeds(self):
        identity = CertIdentity(
            cn=CA_COMMON_NAME,
            sha256_hex="A" * 64,
            sha1_hex="B" * 40,
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            uninstall_ca(identity)

        # Exactly one call: SHA-256 succeeded, no fallback.
        assert mock_run.call_count == 1
        argv = mock_run.call_args.args[0]
        assert argv[:3] == ["security", "delete-certificate", "-Z"]
        # SHA-256 is what we tried.
        assert argv[3] == identity.sha256_hex
        assert "-t" in argv

    def test_sha1_fallback_when_sha256_fails(self):
        identity = CertIdentity(
            cn=CA_COMMON_NAME,
            sha256_hex="A" * 64,
            sha1_hex="B" * 40,
        )
        # First call (SHA-256) fails with a non-not-found code; second
        # call (SHA-1) succeeds.
        responses = [
            _completed(1, stderr="unknown option -Z hash format"),
            _completed(0),
        ]
        with patch("subprocess.run", side_effect=responses) as mock_run:
            uninstall_ca(identity)

        assert mock_run.call_count == 2
        argv1 = mock_run.call_args_list[0].args[0]
        argv2 = mock_run.call_args_list[1].args[0]
        # First argv used SHA-256.
        assert argv1[3] == identity.sha256_hex
        # Second argv used SHA-1.
        assert argv2[3] == identity.sha1_hex

    def test_not_found_treated_as_success(self):
        identity = CertIdentity(
            cn=CA_COMMON_NAME,
            sha256_hex="A" * 64,
            sha1_hex="B" * 40,
        )
        # Exit code 25 is "not found" -- idempotent uninstall.
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed(25, stderr="SecKeychainItemNotFound")
            uninstall_ca(identity)

        # Just one call -- not-found short-circuits the fallback.
        assert mock_run.call_count == 1

    def test_both_hashes_failing_raises(self):
        identity = CertIdentity(
            cn=CA_COMMON_NAME,
            sha256_hex="A" * 64,
            sha1_hex="B" * 40,
        )
        responses = [
            _completed(1, stderr="format"),
            _completed(2, stderr="permission denied"),
        ]
        with patch("subprocess.run", side_effect=responses):
            with pytest.raises(SecurityCommandError) as exc_info:
                uninstall_ca(identity)
        assert exc_info.value.returncode == 2

    def test_missing_identity_falls_back_to_cn(
        self, _redirect_default_confdir, capsys
    ):
        # No ca-identity.json in the (autouse-redirected) DEFAULT_CONFDIR.
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            uninstall_ca(None)

        assert mock_run.call_count == 1
        argv = mock_run.call_args.args[0]
        # CN-based delete, not -Z hash.
        assert argv[:3] == ["security", "delete-certificate", "-c"]
        assert argv[3] == CA_COMMON_NAME

        # Warning printed to console.
        captured = capsys.readouterr()
        assert "ca-identity.json missing" in captured.out

    def test_loads_identity_from_default_confdir_when_none(
        self, _redirect_default_confdir
    ):
        # Pre-populate ca-identity.json.
        identity = CertIdentity(
            cn=CA_COMMON_NAME,
            sha256_hex="C" * 64,
            sha1_hex="D" * 40,
        )
        path = _redirect_default_confdir / "ca-identity.json"
        path.write_text(json.dumps({
            "cn": identity.cn,
            "sha256_hex": identity.sha256_hex,
            "sha1_hex": identity.sha1_hex,
        }))
        os.chmod(path, 0o600)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            uninstall_ca(None)

        argv = mock_run.call_args.args[0]
        assert argv[3] == identity.sha256_hex


# ---------------------------------------------------------------------------
# Integration: install then verify True; uninstall then verify False.
# ---------------------------------------------------------------------------


class TestInstallVerifyUninstallCycle:
    def test_install_then_verify_true_then_uninstall_then_verify_false(
        self, _redirect_default_confdir
    ):
        confdir = _redirect_default_confdir
        cert_pem = generate_ca(confdir)
        identity_holder = {}

        # Phase 1: install.
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            installed = install_ca(cert_pem)
            identity_holder["id"] = installed

        # Verify command sequence: install was a single security call.
        install_argv = mock_run.call_args_list[0].args[0]
        assert install_argv[:2] == ["security", "add-trusted-cert"]

        # Phase 2: verify -- mock find-certificate + dump-trust-settings to
        # report the installed SHA-1 as a trust root.
        sha1 = identity_holder["id"].sha1_hex

        def verify_run(argv, *args, **kwargs):
            if argv[:2] == ["security", "find-certificate"]:
                return _completed(0, stdout=_find_certificate_stdout(sha1))
            if argv[:2] == ["security", "dump-trust-settings"]:
                if "-d" not in argv:
                    return _completed(0, stdout=_trust_settings_stdout(sha1))
                return _completed(1, stdout="")
            return _completed(0)

        with patch("subprocess.run", side_effect=verify_run):
            assert verify_ca() is True

        # Phase 3: uninstall.
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            uninstall_ca(identity_holder["id"])

        # SHA-256 primary path was used.
        del_argv = mock_run.call_args_list[0].args[0]
        assert del_argv[:3] == ["security", "delete-certificate", "-Z"]
        assert del_argv[3] == identity_holder["id"].sha256_hex

        # Phase 4: verify after uninstall -- find-certificate returns no
        # match, so verify_ca returns False.
        def post_uninstall_run(argv, *args, **kwargs):
            if argv[:2] == ["security", "find-certificate"]:
                # Empty stdout = no certs with that CN.
                return _completed(1, stdout="")
            return _completed(0)

        with patch("subprocess.run", side_effect=post_uninstall_run):
            assert verify_ca() is False
