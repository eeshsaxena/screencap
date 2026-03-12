"""Curated test corpus for privacy detection recall/precision benchmarks.

All data is synthetic — no real PII committed to the repo.
Each test case specifies expected detections for measuring recall/precision.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExpectedEntity:
    """An expected entity in a test case."""

    entity_type: str  # EntityType constant
    substring: str  # The text that should be detected
    source: str | None = None  # Expected detector (None = any)


class Frequency:
    """Estimated real-world occurrence rate in a typical recording session.

    HIGH = appears hundreds of times per hour (shell prompts, menu bars, build output)
    MEDIUM = appears tens of times per hour (window titles, sidebar labels)
    LOW = appears a few times per session at most (SSNs, credit cards, API keys)
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class CorpusCase:
    """A single test case for the corpus."""

    id: str
    description: str
    text: str
    expected: list[ExpectedEntity]
    is_false_positive: bool = False  # If True, expected should be empty
    frequency: str | None = None  # Frequency hint for real-world impact weighting


# ---------------------------------------------------------------------------
# 1. Window titles with names
# ---------------------------------------------------------------------------

WINDOW_TITLES = [
    CorpusCase(
        id="wt-01",
        description="Chrome window title with name",
        text="John Doe - Google Chrome",
        expected=[ExpectedEntity("PERSON", "John Doe")],
    ),
    CorpusCase(
        id="wt-02",
        description="Outlook with email in title",
        text="jane.smith@example.org - Outlook",
        expected=[ExpectedEntity("EMAIL", "jane.smith@example.org")],
    ),
    CorpusCase(
        id="wt-03",
        description="Slack with name",
        text="Maria Garcia | Slack",
        expected=[ExpectedEntity("PERSON", "Maria Garcia")],
    ),
    CorpusCase(
        id="wt-04",
        description="Terminal with username (expected miss)",
        text="jose.garcia@MacBook-Pro ~ %",
        expected=[],  # No TLD — not a valid email format
    ),
]

# ---------------------------------------------------------------------------
# 2. Typed credentials
# ---------------------------------------------------------------------------

TYPED_CREDENTIALS = [
    CorpusCase(
        id="cred-01",
        description="Password assignment",
        text="password=hunter2",
        expected=[ExpectedEntity("PASSWORD", "password=hunter2", "regex")],
    ),
    CorpusCase(
        id="cred-02",
        description="AWS access key export",
        text="export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        expected=[ExpectedEntity("API_KEY", "AKIAIOSFODNN7EXAMPLE", "secrets")],
    ),
    CorpusCase(
        id="cred-03",
        description="OpenAI key",
        text="OPENAI_API_KEY=sk-AAAAAAAAAAAAAAAAAAAAT3BlbkFJBBBBBBBBBBBBBBBBBBBB",
        expected=[ExpectedEntity("API_KEY", "sk-AAAAAAAAAAAAAAAAAAAAT3BlbkFJBBBBBBBBBBBBBBBBBBBB", "secrets")],
    ),
    CorpusCase(
        id="cred-04",
        description="GitHub token",
        text="GITHUB_TOKEN=ghp_" + "A" * 36,
        expected=[ExpectedEntity("API_KEY", "ghp_" + "A" * 36, "secrets")],
    ),
    CorpusCase(
        id="cred-05",
        description="PEM private key header",
        text="-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAK...",
        expected=[ExpectedEntity("PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----")],
    ),
    CorpusCase(
        id="cred-06",
        description="JWT token",
        text="token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        expected=[ExpectedEntity("JWT", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")],
    ),
    CorpusCase(
        id="cred-07",
        description="Quoted password with keyword",
        text='password = "supersecret123"',
        expected=[ExpectedEntity("PASSWORD", "supersecret123")],
    ),
    CorpusCase(
        id="cred-08",
        description="Basic auth URL",
        text="https://admin:s3cret@db.example.com/api",
        expected=[ExpectedEntity("PASSWORD", "https://admin:s3cret@db.example.com/api")],
    ),
    CorpusCase(
        id="cred-09",
        description="Bearer token (JWT format)",
        text="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJ0ZXN0IjoiMTIzIn0.abc123def456",
        expected=[ExpectedEntity("JWT", "eyJhbGciOiJIUzI1NiJ9.eyJ0ZXN0IjoiMTIzIn0.", "secrets")],
    ),
    CorpusCase(
        id="cred-10",
        description="Stripe key",
        text="STRIPE_KEY=sk_live_" + "a" * 24,
        expected=[ExpectedEntity("API_KEY", "sk_live_" + "a" * 24, "secrets")],
    ),
]

# ---------------------------------------------------------------------------
# 3. Mixed PII + secrets
# ---------------------------------------------------------------------------

MIXED_PII_SECRETS = [
    CorpusCase(
        id="mix-01",
        description="Name + API key + phone",
        text="Hi John, your API key is sk-AAAAAAAAAAAAAAAAAAAAT3BlbkFJBBBBBBBBBBBBBBBBBBBB. Call me at 555-123-4567.",
        expected=[
            ExpectedEntity("PERSON", "John"),
            ExpectedEntity("API_KEY", "sk-AAAAAAAAAAAAAAAAAAAAT3BlbkFJBBBBBBBBBBBBBBBBBBBB"),
            ExpectedEntity("PHONE", "555-123-4567"),
        ],
    ),
    CorpusCase(
        id="mix-02",
        description="Email + SSN",
        text="Contact john.doe@example.com, SSN: 123-45-6789",
        expected=[
            ExpectedEntity("EMAIL", "john.doe@example.com"),
            ExpectedEntity("SSN", "123-45-6789"),
        ],
    ),
    CorpusCase(
        id="mix-03",
        description="Address + credit card",
        text="Ship to 123 Main St, Anytown, USA 12345. Card: 4532 0151 1283 0366",
        expected=[
            ExpectedEntity("ADDRESS", "123 Main St, Anytown, USA 12345"),
            ExpectedEntity("CREDIT_CARD", "4532 0151 1283 0366"),
        ],
    ),
    CorpusCase(
        id="mix-04",
        description="Name + email in message",
        text="From: Jane Smith <jane.smith@example.org>",
        expected=[
            ExpectedEntity("PERSON", "Jane Smith"),
            ExpectedEntity("EMAIL", "jane.smith@example.org"),
        ],
    ),
]

# ---------------------------------------------------------------------------
# 4. OCR-like noisy text
# ---------------------------------------------------------------------------

OCR_NOISY = [
    CorpusCase(
        id="ocr-01",
        description="Extra spaces in name",
        text="J ohn D oe - Go ogle Chr ome",
        expected=[],  # Hard for PII engines; acceptable miss
    ),
    CorpusCase(
        id="ocr-02",
        description="Password across line break",
        text="password=\nhunter2",
        expected=[],  # May or may not catch across lines
    ),
    CorpusCase(
        id="ocr-03",
        description="Email with OCR noise",
        text="j ohn.doe@example.com",
        expected=[],  # Likely missed due to spacing
    ),
]

# ---------------------------------------------------------------------------
# 5. False positive candidates (should NOT be flagged)
# ---------------------------------------------------------------------------

FALSE_POSITIVES = [
    CorpusCase(
        id="fp-01",
        description="UUID",
        text="request-id: 550e8400-e29b-41d4-a716-446655440000",
        expected=[],
        is_false_positive=True,
    ),
    CorpusCase(
        id="fp-02",
        description="Hex color code",
        text="background-color: #FF5733;",
        expected=[],
        is_false_positive=True,
    ),
    CorpusCase(
        id="fp-03",
        description="Base64 image data",
        text="data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
        expected=[],
        is_false_positive=True,
    ),
    CorpusCase(
        id="fp-04",
        description="Random-looking variable name",
        text="const xQz7kLmNp = calculateHash();",
        expected=[],
        is_false_positive=True,
    ),
    CorpusCase(
        id="fp-05",
        description="Plain text sentence",
        text="The quick brown fox jumps over the lazy dog",
        expected=[],
        is_false_positive=True,
    ),
    CorpusCase(
        id="fp-06",
        description="Git commit hash",
        text="commit a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        expected=[],
        is_false_positive=True,
    ),
    CorpusCase(
        id="fp-07",
        description="Timestamp",
        text="2024-01-15T12:30:45.123Z",
        expected=[],
        is_false_positive=True,
    ),
    CorpusCase(
        id="fp-08",
        description="Version string",
        text="v2.14.3-beta.1+build.456",
        expected=[],
        is_false_positive=True,
    ),
    CorpusCase(
        id="fp-09",
        description="App name that looks like proper noun (Ghostty)",
        text="Ghostty tmux a",
        expected=[],
        is_false_positive=True,
    ),
    CorpusCase(
        id="fp-10",
        description="App name (Bitwarden)",
        text="Bitwarden Bitwarden",
        expected=[],
        is_false_positive=True,
    ),
    CorpusCase(
        id="fp-11",
        description="App name (Homebrew)",
        text="Homebrew — Installing packages",
        expected=[],
        is_false_positive=True,
    ),
    CorpusCase(
        id="fp-12",
        description="App name (Terminal.app)",
        text="Terminal.app — bash",
        expected=[],
        is_false_positive=True,
    ),
]

# ---------------------------------------------------------------------------
# 6. Non-English names
# ---------------------------------------------------------------------------

NON_ENGLISH_NAMES = [
    CorpusCase(
        id="intl-01",
        description="Spanish name",
        text="Jose Garcia logged in",
        expected=[ExpectedEntity("PERSON", "Jose Garcia")],
    ),
    CorpusCase(
        id="intl-02",
        description="German name with umlaut",
        text="From: Hans Müller <hans@example.net>",
        expected=[
            ExpectedEntity("PERSON", "Hans Müller"),
            ExpectedEntity("EMAIL", "hans@example.net"),
        ],
    ),
    CorpusCase(
        id="intl-03",
        description="Chinese name (expected miss with English model)",
        text="User: 张伟 uploaded a file",
        expected=[],  # English spaCy model doesn't detect CJK names
    ),
    CorpusCase(
        id="intl-04",
        description="Japanese name (expected miss with English model)",
        text="田中太郎 sent a message",
        expected=[],  # English spaCy model doesn't detect CJK names
    ),
]

# ---------------------------------------------------------------------------
# 7. Connection strings
# ---------------------------------------------------------------------------

CONNECTION_STRINGS = [
    CorpusCase(
        id="conn-01",
        description="PostgreSQL connection string",
        text="DATABASE_URL=postgresql://admin:s3cret@db.example.com:5432/myapp",
        expected=[ExpectedEntity("CONNECTION_STRING", "postgresql://admin:s3cret@db.example.com:5432/myapp")],
    ),
    CorpusCase(
        id="conn-02",
        description="MongoDB connection string",
        text="MONGO_URI=mongodb+srv://user:pass123@cluster.mongodb.net/production",
        expected=[ExpectedEntity("CONNECTION_STRING", "mongodb+srv://user:pass123@cluster.mongodb.net/production")],
    ),
    CorpusCase(
        id="conn-03",
        description="Redis connection string",
        text="REDIS_URL=redis://default:password@redis.example.com:6379",
        expected=[ExpectedEntity("CONNECTION_STRING", "redis://default:password@redis.example.com:6379")],
    ),
]

# ---------------------------------------------------------------------------
# 8. Deeply nested JSON strings (simulating element_state)
# ---------------------------------------------------------------------------

NESTED_JSON = [
    CorpusCase(
        id="json-01",
        description="Email in JSON value",
        text='{"user": {"email": "alice@example.com", "name": "Alice Johnson"}}',
        expected=[
            ExpectedEntity("EMAIL", "alice@example.com"),
            ExpectedEntity("PERSON", "Alice Johnson"),
        ],
    ),
    CorpusCase(
        id="json-02",
        description="Password in JSON",
        text='{"config": {"password": "db_secret_123"}}',
        expected=[ExpectedEntity("PASSWORD", "db_secret_123")],
    ),
]

# ---------------------------------------------------------------------------
# 9. Additional PII cases
# ---------------------------------------------------------------------------

ADDITIONAL_PII = [
    CorpusCase(
        id="pii-01",
        description="US phone number",
        text="Call me at (555) 123-4567",
        expected=[ExpectedEntity("PHONE", "(555) 123-4567")],
    ),
    CorpusCase(
        id="pii-02",
        description="SSN",
        text="SSN: 123-45-6789",
        expected=[ExpectedEntity("SSN", "123-45-6789")],
    ),
    CorpusCase(
        id="pii-03",
        description="Email address",
        text="Send to user@example.com please",
        expected=[ExpectedEntity("EMAIL", "user@example.com")],
    ),
    CorpusCase(
        id="pii-04",
        description="Credit card (Luhn valid, spaced)",
        text="Payment card: 4532 0151 1283 0366",
        expected=[ExpectedEntity("CREDIT_CARD", "4532 0151 1283 0366")],
    ),
    CorpusCase(
        id="pii-05",
        description="Full name in sentence",
        text="Dear Robert Johnson, your account has been updated.",
        expected=[ExpectedEntity("PERSON", "Robert Johnson")],
    ),
    CorpusCase(
        id="pii-06",
        description="Multiple emails",
        text="CC: alice@example.com, bob@example.org",
        expected=[
            ExpectedEntity("EMAIL", "alice@example.com"),
            ExpectedEntity("EMAIL", "bob@example.org"),
        ],
    ),
]

# ---------------------------------------------------------------------------
# 10. Edge cases
# ---------------------------------------------------------------------------

EDGE_CASES = [
    CorpusCase(
        id="edge-01",
        description="Short text (< 4 chars)",
        text="hi",
        expected=[],
    ),
    CorpusCase(
        id="edge-02",
        description="Empty text",
        text="",
        expected=[],
    ),
    CorpusCase(
        id="edge-03",
        description="Zero-width chars around password",
        text="p\u200bassword=hunter2",
        expected=[ExpectedEntity("PASSWORD", "password=hunter2")],
    ),
    CorpusCase(
        id="edge-04",
        description="NFKC fullwidth chars",
        text="\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44=secret",
        expected=[ExpectedEntity("PASSWORD", "password=secret")],
    ),
]


# ---------------------------------------------------------------------------
# 11. Terminal / accessibility text (false positive candidates)
# ---------------------------------------------------------------------------

TERMINAL_ACCESSIBILITY = [
    # --- Terminal output (FP candidates) ---
    CorpusCase(
        id="term-01",
        description="zsh prompt with username",
        text="jose@MacBook-Pro screencap %",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.HIGH,  # Every keystroke triggers a prompt capture
    ),
    CorpusCase(
        id="term-02",
        description="bash prompt with path",
        text="user@host:/var/log$ grep ERROR app.log",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.HIGH,
    ),
    CorpusCase(
        id="term-03",
        description="brew list output",
        text="cairo ffmpeg ghostscript harfbuzz libpng",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.MEDIUM,
    ),
    CorpusCase(
        id="term-04",
        description="pip install output",
        text=(
            "Collecting presidio-analyzer>=2.2\n"
            "  Downloading presidio_analyzer-2.2.355-py3-none-any.whl"
        ),
        expected=[],
        is_false_positive=True,
        frequency=Frequency.MEDIUM,
    ),
    CorpusCase(
        id="term-05",
        description="docker ps output",
        text=(
            "CONTAINER ID  IMAGE          COMMAND    CREATED    STATUS    PORTS    NAMES\n"
            "a1b2c3d4e5f6  postgres:15    postgres   2 hours    Up 2h     5432     my-db"
        ),
        expected=[],
        is_false_positive=True,
        frequency=Frequency.MEDIUM,
    ),
    CorpusCase(
        id="term-06",
        description="git log --oneline",
        text="a1b2c3d fix(privacy): prevent app_classes bypass\n34d0845 Merge pull request #83",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.HIGH,  # git log appears in every terminal session
    ),
    CorpusCase(
        id="term-07",
        description="Python traceback",
        text=(
            'File "/usr/lib/python3.12/json/decoder.py", line 355, in raw_decode\n'
            '    raise JSONDecodeError("Expecting value", s, err.value)'
        ),
        expected=[],
        is_false_positive=True,
        frequency=Frequency.HIGH,  # Tracebacks during dev are constant
    ),
    CorpusCase(
        id="term-08",
        description="npm run output",
        text="> screencap@0.10.0 build\n> tsc --build tsconfig.json",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.HIGH,
    ),
    CorpusCase(
        id="term-09",
        description="ls -la output",
        text="-rw-r--r--  1 jose  staff  4096 Mar 12 10:30 config.toml",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.HIGH,
    ),
    CorpusCase(
        id="term-10",
        description="ps aux output",
        text="jose     12345  0.0  0.1  408628  16384 s001  S    10:30AM   0:00.05 /usr/bin/python3",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.MEDIUM,
    ),
    CorpusCase(
        id="term-11",
        description="Box-drawing characters",
        text="┌─────────────────────────┐\n│  Settings               │\n└─────────────────────────┘",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.MEDIUM,  # TUI apps (htop, lazygit, etc.)
    ),
    CorpusCase(
        id="term-12",
        description="env output (no secrets)",
        text="SHELL=/bin/zsh\nHOME=/Users/jose\nPATH=/usr/local/bin:/usr/bin:/bin",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.MEDIUM,
    ),
    CorpusCase(
        id="term-13",
        description="make build output",
        text="gcc -Wall -O2 -o main main.c utils.c\ncc1: all warnings being treated as errors",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.HIGH,
    ),
    CorpusCase(
        id="term-14",
        description="pytest output with test names",
        text="tests/test_cli.py::test_start_recording PASSED\ntests/test_cli.py::test_stop_recording PASSED",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.HIGH,
    ),
    CorpusCase(
        id="term-15",
        description="kubectl get pods output",
        text="NAME                     READY   STATUS    RESTARTS   AGE\napi-server-7f8b9c6d4-x2k  1/1     Running   0          3d",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.MEDIUM,
    ),
    CorpusCase(
        id="term-16",
        description="cargo build output",
        text="   Compiling serde v1.0.197\n   Compiling tokio v1.36.0\n    Finished `release` profile [optimized] target(s) in 45.32s",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.HIGH,
    ),

    # --- Real PII in terminal contexts (true positives) ---
    CorpusCase(
        id="term-tp-01",
        description="git config with real email",
        text="git config user.email jose.garcia@example.com",
        expected=[ExpectedEntity("EMAIL", "jose.garcia@example.com")],
    ),
    CorpusCase(
        id="term-tp-02",
        description="export PASSWORD",
        text="export DATABASE_PASSWORD=mysecretpassword123",
        expected=[ExpectedEntity("PASSWORD", "DATABASE_PASSWORD=mysecretpassword123")],
    ),
    CorpusCase(
        id="term-tp-03",
        description="env with API key",
        text="OPENAI_API_KEY=sk-AAAAAAAAAAAAAAAAAAAAT3BlbkFJBBBBBBBBBBBBBBBBBBBB",
        expected=[ExpectedEntity("API_KEY", "sk-AAAAAAAAAAAAAAAAAAAAT3BlbkFJBBBBBBBBBBBBBBBBBBBB", "secrets")],
    ),
    CorpusCase(
        id="term-tp-04",
        description="curl with auth header (JWT format)",
        text="curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJ0ZXN0IjoiMTIzIn0.abc123def456' https://api.example.com",
        expected=[ExpectedEntity("JWT", "eyJhbGciOiJIUzI1NiJ9.eyJ0ZXN0IjoiMTIzIn0.", "secrets")],
    ),
    # term-tp-05 removed: "ssh admin@192.168.1.100" is an SSH target,
    # not an email address. No detector should flag user@host as EMAIL.
    CorpusCase(
        id="term-tp-06",
        description="AWS secret access key in env",
        text="AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        expected=[ExpectedEntity("API_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")],
    ),

    # --- Harder mixed cases ---
    CorpusCase(
        id="term-hard-01",
        description="URL with embedded credentials",
        text="psql postgresql://admin:s3cretP@ss@db.prod.example.com:5432/myapp",
        expected=[ExpectedEntity("CONNECTION_STRING", "postgresql://admin:s3cretP@ss@db.prod.example.com:5432/myapp")],
    ),
    CorpusCase(
        id="term-hard-02",
        description="Mixed secrets + noise in env dump",
        text="LANG=en_US.UTF-8\nAWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\nTERM=xterm-256color",
        expected=[ExpectedEntity("API_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")],
    ),
    CorpusCase(
        id="term-hard-03",
        description="Git diff with email added",
        text='+    "email": "alice.jones@example.org",\n-    "email": "placeholder@example.net",',
        expected=[
            ExpectedEntity("EMAIL", "alice.jones@example.org"),
            ExpectedEntity("EMAIL", "placeholder@example.net"),
        ],
    ),
]

# ---------------------------------------------------------------------------
# 12. OCR screenshot false positives
# ---------------------------------------------------------------------------

OCR_SCREENSHOT_FPS = [
    CorpusCase(
        id="ocr-fp-01",
        description="Gmail sidebar labels",
        text="Inbox Starred Snoozed Sent Drafts More",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.HIGH,  # Every screenshot of Gmail
    ),
    CorpusCase(
        id="ocr-fp-02",
        description="macOS menu bar items",
        text="File Edit View Window Help",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.HIGH,  # Visible in every screenshot
    ),
    CorpusCase(
        id="ocr-fp-03",
        description="Browser tab titles concatenated",
        text="GitHub PRs Google Docs Figma Settings",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.HIGH,
    ),
    CorpusCase(
        id="ocr-fp-04",
        description="Calendar month abbreviations",
        text="Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.MEDIUM,  # Calendar views
    ),
    CorpusCase(
        id="ocr-fp-05",
        description="Finder toolbar buttons",
        text="Back Forward View Group Share Edit Tags",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.MEDIUM,
    ),
    CorpusCase(
        id="ocr-fp-06",
        description="Dock app names",
        text="Safari Mail Maps Messages FaceTime Calendar Notes",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.HIGH,  # Dock visible in most screenshots
    ),
    CorpusCase(
        id="ocr-fp-07",
        description="VS Code status bar",
        text="UTF-8  LF  Python  Ln 42, Col 15  Spaces: 4",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.HIGH,  # Visible while coding
    ),
    CorpusCase(
        id="ocr-fp-08",
        description="Xcode build status",
        text="Build Succeeded | 12 warnings | MyApp.app",
        expected=[],
        is_false_positive=True,
        frequency=Frequency.MEDIUM,
    ),
]


# ---------------------------------------------------------------------------
# All test cases combined
# ---------------------------------------------------------------------------

ALL_TEST_CASES: list[CorpusCase] = (
    WINDOW_TITLES
    + TYPED_CREDENTIALS
    + MIXED_PII_SECRETS
    + OCR_NOISY
    + FALSE_POSITIVES
    + NON_ENGLISH_NAMES
    + CONNECTION_STRINGS
    + NESTED_JSON
    + ADDITIONAL_PII
    + EDGE_CASES
    + TERMINAL_ACCESSIBILITY
    + OCR_SCREENSHOT_FPS
)

# Count for verification
TRUE_POSITIVE_CASES = [tc for tc in ALL_TEST_CASES if not tc.is_false_positive and tc.expected]
FALSE_POSITIVE_CASES = [tc for tc in ALL_TEST_CASES if tc.is_false_positive]
