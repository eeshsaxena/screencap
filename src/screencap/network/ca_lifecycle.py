"""CA lifecycle for the screencap mitmproxy addon (V1).

Generates a 30-day root CA via :mod:`cryptography`, installs it as a
trusted root in the user's login keychain via macOS ``security`` CLI,
verifies presence + trust-anchor status + expiry, and uninstalls by
fingerprint (SHA-256 primary, SHA-1 fallback for older macOS).

V1 is CA-only: no KEK, no DEK, no keyring, no body encryption. V1.5
will add a sibling ``crypto.py`` module -- leave it alone here.

Hardening:
- Combined PEM (private key + cert) at ``<confdir>/mitmproxy-ca.pem``,
  chmod 600.
- Cert-only PEM at ``<confdir>/mitmproxy-ca-cert.pem``, chmod 600.
- ``ca-identity.json`` at ``<confdir>/ca-identity.json``, chmod 600,
  pinning the install-time SHA-256 + SHA-1 for unambiguous uninstall
  across rotations.
- ``<confdir>`` itself is mode 0o700 (owner-only listable).
- ``com.apple.metadata:com_apple_backup_excludeItem`` xattr set on the
  confdir to keep the CA private key out of Time Machine. iCloud-Drive
  sync is detected via path-existence checks and a warning is printed
  (xattr does NOT block iCloud sync).

The actual ``security`` invocations follow the External References in
the plan: ``add-trusted-cert -r trustRoot -k <login>`` for install,
``find-certificate -c <CN> -a -Z <login>`` + ``dump-trust-settings``
(user AND admin) for verify, ``delete-certificate -Z <hash> -t <login>``
for uninstall.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console

console = Console()


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


CA_COMMON_NAME = "screencap proxy CA"
"""Subject CN baked into the generated cert. Used by ``find-certificate -c``
and the CN-based delete fallback. Single source of truth across V1 + V1.5."""

DEFAULT_CONFDIR = Path("~/.screencap/proxy").expanduser()
"""Default location of the mitmproxy CA + identity.json."""

LOGIN_KEYCHAIN = Path("~/Library/Keychains/login.keychain-db").expanduser()
"""Per-user keychain ``security`` operates on. Distinct from the root
``System.keychain`` (which would require sudo)."""

_BACKUP_EXCLUDE_XATTR = "com.apple.metadata:com_apple_backup_excludeItem"
_BACKUP_EXCLUDE_VALUE = "com.apple.backupd"

_NOT_FOUND_EXIT_CODES = {25, 26, 50}
"""Exit codes ``security`` uses for "no such certificate". Treated as
success during uninstall (idempotent). The set is broader than strictly
necessary -- older macOS releases have used different codes for the same
"errSecItemNotFound" condition."""


# ---------------------------------------------------------------------------
# Types & exceptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CertIdentity:
    """Install-time fingerprint pin for a screencap CA cert.

    Both hashes are recorded so uninstall can use SHA-256 by default and
    fall back to SHA-1 on older macOS that doesn't accept SHA-256 with
    ``security delete-certificate -Z``. Hex strings are uppercase, no
    separator.
    """

    cn: str
    sha256_hex: str
    sha1_hex: str


class SecurityCommandError(RuntimeError):
    """Raised when a ``security`` subprocess invocation fails.

    Carries the failed argv, exit code, and stderr so the pre-flight UI
    can surface an actionable message.
    """

    def __init__(
        self,
        argv: list[str],
        returncode: int,
        stderr: str,
    ) -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"security command failed (exit {returncode}): "
            f"{' '.join(argv)}\nstderr: {stderr.strip()}"
        )


# ---------------------------------------------------------------------------
# Filesystem hardening primitives
# ---------------------------------------------------------------------------


def chmod_600_or_raise(path: Path) -> None:
    """Ensure ``path`` has mode 0o600 and is owned by the current user.

    Idempotent: if the file is already mode 0o600 and owned by the
    current user, this is a no-op. Otherwise:

    * If owned by another uid -> raise :class:`PermissionError` with an
      actionable message (don't try to chmod something we don't own --
      the chmod will likely succeed on macOS for the owner-equivalent
      ACL but leaves a confusing trail).
    * If owned by us with a wider mode -> chmod 0o600 in place.
    """
    if not path.exists():
        raise FileNotFoundError(f"chmod_600_or_raise: {path} does not exist")
    st = path.stat()
    current_uid = os.getuid()
    if st.st_uid != current_uid:
        raise PermissionError(
            f"{path} is owned by uid {st.st_uid}, not the current user "
            f"(uid {current_uid}). Refusing to chmod a file we don't own. "
            f"Run `sudo chown $(id -u) {path}` and retry, or remove the file."
        )
    current_mode = st.st_mode & 0o777
    if current_mode != 0o600:
        os.chmod(path, 0o600)


def mkdir_700_or_raise(path: Path) -> None:
    """Create ``path`` with mode 0o700, idempotently.

    If the directory already exists with a different mode, attempt
    ``chmod 0o700`` (only if owned by the current user) or raise.

    The default umask 0o022 produces 0o755 dirs that are world-listable --
    chmod-700 closes that gap on multi-user Macs and prevents directory
    listing from leaking the presence of CA material or recording IDs.
    """
    if path.exists():
        if not path.is_dir():
            raise NotADirectoryError(
                f"{path} exists but is not a directory; refusing to "
                f"convert it. Move or remove it and retry."
            )
        st = path.stat()
        current_uid = os.getuid()
        if st.st_uid != current_uid:
            raise PermissionError(
                f"{path} is owned by uid {st.st_uid}, not the current "
                f"user (uid {current_uid}). Refusing to chmod a directory "
                f"we don't own."
            )
        current_mode = st.st_mode & 0o777
        if current_mode != 0o700:
            os.chmod(path, 0o700)
        return

    # Doesn't exist -- create with parents, then chmod (the mode= kwarg on
    # mkdir is honored only on the leaf, and the umask applies; explicit
    # chmod is the simplest way to guarantee 0o700 on the leaf).
    path.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(path, 0o700)


def set_backup_exclude(path: Path) -> None:
    """Mark ``path`` to be skipped by Time Machine.

    Sets the ``com.apple.metadata:com_apple_backup_excludeItem`` xattr
    via the ``xattr`` shell command (the Python ``xattr`` package is not
    a dependency, and ``os.setxattr`` is Linux-only on macOS Python).
    Best-effort: failures are logged but do NOT raise, because the
    xattr is hardening, not correctness -- the chmod-600 protection on
    the file itself is the primary control.

    This does NOT block iCloud Drive sync -- that requires moving the
    file out of the synced location. See :func:`check_icloud_sync`.
    """
    if not path.exists():
        # Quietly skip -- orchestrator may call us before the dir exists
        # in some refactors. Don't surface a confusing error here.
        return
    try:
        result = subprocess.run(
            [
                "xattr",
                "-w",
                _BACKUP_EXCLUDE_XATTR,
                _BACKUP_EXCLUDE_VALUE,
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            console.print(
                f"[yellow]warning:[/yellow] failed to set backup-exclude "
                f"xattr on {path}: {result.stderr.strip() or 'unknown error'}"
            )
    except FileNotFoundError:
        # ``xattr`` binary missing -- extremely unusual on macOS but
        # don't blow up the install.
        console.print(
            "[yellow]warning:[/yellow] `xattr` command not found; "
            "skipping Time Machine backup exclusion."
        )


def check_icloud_sync(path: Path) -> bool:
    """Return True if ``path`` is under an iCloud-Drive-synced location.

    Three detection paths:

    1. ``path`` resolves under ``~/Library/Mobile Documents/`` -- direct
       iCloud Drive content path.
    2. ``~/Library/Mobile Documents/com~apple~CloudDocs/Desktop/``
       exists (iCloud Desktop sync is active) AND ``path`` resolves
       under ``~/Desktop/``.
    3. ``~/Library/Mobile Documents/com~apple~CloudDocs/Documents/``
       exists (iCloud Documents sync is active) AND ``path`` resolves
       under ``~/Documents/``.

    Path-existence checks ONLY -- see plan lines 619-620 for the
    rationale (``defaults read`` is unreliable across macOS 12-15).
    """
    try:
        resolved = path.resolve()
    except OSError:
        return False

    home = Path("~").expanduser().resolve()
    mobile_docs = home / "Library" / "Mobile Documents"

    # Case 1: directly under Mobile Documents.
    try:
        resolved.relative_to(mobile_docs)
        return True
    except ValueError:
        pass

    # Cases 2 & 3: Desktop / Documents sync.
    sync_pairs = [
        (mobile_docs / "com~apple~CloudDocs" / "Desktop", home / "Desktop"),
        (mobile_docs / "com~apple~CloudDocs" / "Documents", home / "Documents"),
    ]
    for sync_marker, user_dir in sync_pairs:
        if not sync_marker.exists():
            continue
        try:
            resolved.relative_to(user_dir.resolve())
            return True
        except (ValueError, OSError):
            continue

    return False


def setup_proxy_dir(confdir: Path = DEFAULT_CONFDIR) -> Path:
    """Prepare ``confdir`` for CA + snapshot storage.

    * Creates ``confdir`` and ``confdir/snapshots`` with mode 0o700.
    * Sets the Time Machine backup-exclude xattr on ``confdir``.
    * Warns (does not block) if ``confdir`` is under an iCloud-synced
      path.

    Returns the (possibly newly-created) confdir path for chaining.
    """
    confdir = Path(confdir)
    mkdir_700_or_raise(confdir)
    mkdir_700_or_raise(confdir / "snapshots")
    set_backup_exclude(confdir)
    if check_icloud_sync(confdir):
        console.print(
            f"[yellow]Warning:[/yellow] {confdir} is under an "
            f"iCloud-synced location. The CA private key will be "
            f"uploaded to iCloud Drive. Move ~/.screencap to a "
            f"non-synced location (e.g. ~/Library/Application Support) "
            f"or disable iCloud Drive's Desktop & Documents sync."
        )
    return confdir


# ---------------------------------------------------------------------------
# CA generation
# ---------------------------------------------------------------------------


def generate_ca(confdir: Path, days: int = 30) -> Path:
    """Generate a fresh 30-day RSA-2048 root CA into ``confdir``.

    Writes two PEM files:

    * ``<confdir>/mitmproxy-ca.pem`` -- combined private key + cert,
      mode 0o600. This is the path mitmproxy expects.
    * ``<confdir>/mitmproxy-ca-cert.pem`` -- cert-only PEM, mode 0o600.

    Returns the combined-PEM path. Caller is responsible for calling
    :func:`setup_proxy_dir` first to ensure the dir is mode 0o700 + has
    the backup-exclude xattr.

    Raises :class:`PermissionError` with an actionable message if the
    confdir is read-only (typical surprise: user ran ``screencap``
    under sudo once and ``~/.screencap`` ended up root-owned).
    """
    # Heavy imports deferred to function body -- keeps `screencap --help`
    # fast for the non-network path. cryptography is a transitive dep
    # of mitmproxy so it's installed but ~80ms cold-start.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    confdir = Path(confdir)
    if not confdir.is_dir():
        raise FileNotFoundError(
            f"generate_ca: confdir {confdir} does not exist or is not a "
            f"directory. Call setup_proxy_dir() first."
        )

    combined_pem = confdir / "mitmproxy-ca.pem"
    cert_pem = confdir / "mitmproxy-ca-cert.pem"

    # Sanity: confirm we can write before spending CPU on RSA gen.
    if not os.access(confdir, os.W_OK):
        raise PermissionError(
            f"generate_ca: confdir {confdir} is not writable by the "
            f"current user. Check ownership (`ls -ld {confdir}`); if "
            f"root-owned, run `sudo chown -R $(id -u):$(id -g) "
            f"{confdir.parent}` and retry."
        )

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    now = datetime.now(timezone.utc)
    not_valid_before = now - timedelta(minutes=1)  # clock skew tolerance
    not_valid_after = now + timedelta(days=days)

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "screencap"),
        ]
    )

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_valid_before)
        .not_valid_after(not_valid_after)
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
            critical=False,
        )
    )

    cert = builder.sign(private_key=private_key, algorithm=hashes.SHA256())

    key_pem_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem_bytes = cert.public_bytes(serialization.Encoding.PEM)

    cert_pem.write_bytes(cert_pem_bytes)
    chmod_600_or_raise(cert_pem)

    combined_pem.write_bytes(key_pem_bytes + cert_pem_bytes)
    chmod_600_or_raise(combined_pem)

    return combined_pem


def is_expiring_soon(threshold_days: int = 7) -> bool:
    """Return True if ``mitmproxy-ca.pem`` expires within ``threshold_days``.

    Reads the cert from the default confdir. Used by the pre-flight to
    decide whether to rotate before starting a new ``--network``
    recording. Returns False if the cert file does not exist (the caller
    should run :func:`generate_ca` instead).
    """
    from cryptography import x509

    cert_path = DEFAULT_CONFDIR / "mitmproxy-ca-cert.pem"
    if not cert_path.exists():
        # Try the combined PEM -- load_pem_x509_certificate skips the key.
        cert_path = DEFAULT_CONFDIR / "mitmproxy-ca.pem"
        if not cert_path.exists():
            return False

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    not_after = cert.not_valid_after_utc
    now = datetime.now(timezone.utc)
    return (not_after - now) < timedelta(days=threshold_days)


# ---------------------------------------------------------------------------
# Identity computation + persistence
# ---------------------------------------------------------------------------


def _compute_identity(cert_pem_bytes: bytes) -> CertIdentity:
    """Parse ``cert_pem_bytes`` and return a :class:`CertIdentity`.

    SHA-1 + SHA-256 are computed over the DER-encoded form of the cert
    (matches ``security``'s on-disk representation; ``find-certificate``
    prints SHA-1 of the DER bytes, ``delete-certificate -Z`` accepts
    both).
    """
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    cert = x509.load_pem_x509_certificate(cert_pem_bytes)
    der = cert.public_bytes(Encoding.DER)
    sha1_hex = hashlib.sha1(der).hexdigest().upper()
    sha256_hex = hashlib.sha256(der).hexdigest().upper()
    return CertIdentity(cn=CA_COMMON_NAME, sha256_hex=sha256_hex, sha1_hex=sha1_hex)


def _identity_path(confdir: Path) -> Path:
    return confdir / "ca-identity.json"


def _save_identity(identity: CertIdentity, confdir: Path) -> Path:
    """Persist ``identity`` to ``<confdir>/ca-identity.json`` with mode 0o600."""
    path = _identity_path(confdir)
    payload = json.dumps(asdict(identity), indent=2, sort_keys=True)
    path.write_text(payload, encoding="utf-8")
    chmod_600_or_raise(path)
    return path


def _load_identity(confdir: Path) -> CertIdentity | None:
    """Read a previously-persisted identity JSON, or return ``None``."""
    path = _identity_path(confdir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    try:
        return CertIdentity(
            cn=data["cn"],
            sha256_hex=data["sha256_hex"],
            sha1_hex=data["sha1_hex"],
        )
    except (KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Install / verify / uninstall
# ---------------------------------------------------------------------------


def install_ca(cert_path: Path) -> CertIdentity:
    """Install ``cert_path`` as a trusted root in the user's login keychain.

    Runs ``security add-trusted-cert -r trustRoot -k <login> <cert>``.
    The ``-d`` (admin domain) flag is intentionally omitted to keep the
    install at the per-user level -- no admin password prompt, no
    system-wide trust.

    Computes :class:`CertIdentity` (SHA-1 + SHA-256 over the DER form
    of the cert) and persists it to ``ca-identity.json`` next to the
    cert. The persisted identity is the unambiguous key for uninstall.

    Raises :class:`SecurityCommandError` carrying stderr if the
    ``security`` invocation fails.
    """
    cert_path = Path(cert_path)
    if not cert_path.exists():
        raise FileNotFoundError(f"install_ca: cert {cert_path} does not exist")

    confdir = cert_path.parent
    cert_pem_bytes = cert_path.read_bytes()
    identity = _compute_identity(cert_pem_bytes)

    argv = [
        "security",
        "add-trusted-cert",
        "-r",
        "trustRoot",
        "-k",
        str(LOGIN_KEYCHAIN),
        str(cert_path),
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SecurityCommandError(argv, result.returncode, result.stderr or "")

    _save_identity(identity, confdir)
    return identity


def _security_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Thin wrapper around ``subprocess.run`` for ``security`` invocations."""
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _parse_sha1_lines(output: str) -> list[str]:
    """Extract uppercase SHA-1 hex values from ``security`` output.

    ``find-certificate -Z`` and ``dump-trust-settings`` both print
    ``SHA-1 hash: <hex>`` lines; the hex is uppercase, no separators.
    """
    return [
        m.group(1).upper()
        for m in re.finditer(r"SHA-1 hash:\s*([0-9A-Fa-f]{40})", output)
    ]


def verify_ca() -> bool:
    """Return True iff the screencap CA is installed AND trusted AND valid.

    Steps:

    1. Load ``mitmproxy-ca.pem`` from the default confdir and recompute
       its SHA-1 locally (don't trust ``ca-identity.json``).
    2. ``security find-certificate -c "<CN>" -a -Z <login>`` -- confirm
       the locally-computed SHA-1 IS present AND that exactly one cert
       with our CN is in the keychain.
    3. ``security dump-trust-settings`` (user) AND ``-d`` (admin) -- the
       SHA-1 must appear in EITHER domain alongside
       ``kSecTrustSettingsResult = kSecTrustSettingsResultTrustRoot``.
    4. ``cert.not_valid_after_utc`` -- confirm we're inside the validity
       window (a cert that's expired is "installed" but unusable).

    All four steps must pass. Any subprocess failure -> False (we are
    explicitly NOT raising here; caller wants a clean True/False).
    """
    from cryptography import x509

    cert_path = DEFAULT_CONFDIR / "mitmproxy-ca.pem"
    if not cert_path.exists():
        return False

    cert_pem_bytes = cert_path.read_bytes()
    try:
        identity = _compute_identity(cert_pem_bytes)
        cert = x509.load_pem_x509_certificate(cert_pem_bytes)
    except Exception:
        return False

    # Step 2: presence in keychain.
    find_argv = [
        "security",
        "find-certificate",
        "-c",
        CA_COMMON_NAME,
        "-a",
        "-Z",
        str(LOGIN_KEYCHAIN),
    ]
    find_result = _security_run(find_argv)
    if find_result.returncode != 0:
        return False

    found_sha1s = _parse_sha1_lines(find_result.stdout)
    if identity.sha1_hex not in found_sha1s:
        return False
    # Exactly-one assertion: a stale duplicate is a verify-fail signal,
    # not a verify-pass -- caller should run uninstall first.
    if found_sha1s.count(identity.sha1_hex) != 1:
        return False
    # Catches the "attacker pre-planted a same-CN cert" case.
    if len(found_sha1s) != 1:
        return False

    # Step 3: trust-anchor status. Accept presence in either domain.
    if not _has_trust_root(identity.sha1_hex, admin=False):
        if not _has_trust_root(identity.sha1_hex, admin=True):
            return False

    # Step 4: expiry.
    now = datetime.now(timezone.utc)
    if cert.not_valid_after_utc <= now:
        return False
    if cert.not_valid_before_utc > now:
        return False

    return True


def _has_trust_root(sha1_hex: str, *, admin: bool) -> bool:
    """Return True if ``sha1_hex`` is a trust-root in the requested domain.

    ``security dump-trust-settings`` (user) or ``-d`` (admin) prints a
    block per cert with ``SHA-1 hash:`` and (for trust roots)
    ``kSecTrustSettingsResult = kSecTrustSettingsResultTrustRoot``.

    We split the output on the per-cert blocks (separated by lines like
    ``Cert N: <CN>``) and confirm both markers appear in the same block
    as our hash.
    """
    argv = ["security", "dump-trust-settings"]
    if admin:
        argv.append("-d")
    result = _security_run(argv)
    if result.returncode != 0:
        return False

    blocks = re.split(r"^Cert \d+:.*$", result.stdout, flags=re.MULTILINE)
    for block in blocks:
        sha1s = _parse_sha1_lines(block)
        if sha1_hex not in sha1s:
            continue
        if "kSecTrustSettingsResultTrustRoot" in block:
            return True
    return False


def uninstall_ca(identity: CertIdentity | None = None) -> None:
    """Remove the screencap CA from the user's login keychain.

    Lookup policy (single source of truth across V1 + V1.5 + Unit 8):

    1. Load ``CertIdentity`` from ``<confdir>/ca-identity.json`` if
       ``identity`` was not passed in.
    2. Try ``security delete-certificate -Z <sha256> -t <login>``
       FIRST. SHA-256 is the modern hash, recorded at install time.
    3. If exit indicates SHA-256-not-understood (older macOS), retry
       with ``-Z <sha1>``.
    4. If the identity JSON is missing, fall back to CN-based delete
       (``-c "screencap proxy CA"``) and warn the user.
    5. Treat "not found" exit codes (25, 26, 50) as success -- uninstall
       is idempotent.
    """
    if identity is None:
        identity = _load_identity(DEFAULT_CONFDIR)

    if identity is None:
        # No identity JSON -> CN-based fallback. The CN delete removes
        # ALL certs with the screencap CN, which is acceptable: there
        # should never be more than one, and if there are stale
        # duplicates, a CN-based sweep is the cleanup we want.
        console.print(
            "[yellow]warning:[/yellow] ca-identity.json missing; "
            "falling back to CN-based delete. This removes ALL certs "
            f"with CN={CA_COMMON_NAME!r} from your login keychain."
        )
        argv = [
            "security",
            "delete-certificate",
            "-c",
            CA_COMMON_NAME,
            "-t",
            str(LOGIN_KEYCHAIN),
        ]
        result = _security_run(argv)
        if result.returncode != 0 and result.returncode not in _NOT_FOUND_EXIT_CODES:
            raise SecurityCommandError(argv, result.returncode, result.stderr or "")
        return

    # SHA-256 primary.
    sha256_argv = [
        "security",
        "delete-certificate",
        "-Z",
        identity.sha256_hex,
        "-t",
        str(LOGIN_KEYCHAIN),
    ]
    result = _security_run(sha256_argv)
    if result.returncode == 0:
        return
    if result.returncode in _NOT_FOUND_EXIT_CODES:
        return

    # SHA-1 fallback. We don't try to read the exact error string --
    # the error text is undocumented and varies across macOS releases.
    # Treat any non-success / non-not-found as a SHA-256-unsupported
    # signal and retry once with SHA-1.
    sha1_argv = [
        "security",
        "delete-certificate",
        "-Z",
        identity.sha1_hex,
        "-t",
        str(LOGIN_KEYCHAIN),
    ]
    result = _security_run(sha1_argv)
    if result.returncode == 0:
        return
    if result.returncode in _NOT_FOUND_EXIT_CODES:
        return
    raise SecurityCommandError(sha1_argv, result.returncode, result.stderr or "")


__all__ = [
    "CA_COMMON_NAME",
    "DEFAULT_CONFDIR",
    "CertIdentity",
    "SecurityCommandError",
    "chmod_600_or_raise",
    "mkdir_700_or_raise",
    "set_backup_exclude",
    "check_icloud_sync",
    "setup_proxy_dir",
    "generate_ca",
    "is_expiring_soon",
    "install_ca",
    "verify_ca",
    "uninstall_ca",
]
