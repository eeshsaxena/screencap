---
name: screencap-redaction
description: Decision-making guide and reference for PII/secrets redaction in the screencap macOS recording tool. Use when working on scrubber.py, privacy features, redaction pipeline design, or evaluating redaction tools/approaches. Triggers on redaction architecture, PII detection, secrets scanning, OCR-based scrubbing, audio redaction, accessibility-based redaction, or any privacy-related feature work in screencap.
---

# Screencap Redaction Guide

## Overview

This skill encodes research findings from comprehensive deep-dives (ChatGPT Deep Research, Gemini analysis, Claude Deep Research) into macOS screen recording redaction. It serves as a decision-making aid when choosing tools, architectures, and approaches for PII and secrets redaction across all captured modalities: screenshots, video, audio/transcripts, text/DB fields, and accessibility data.

## Current State (as of v0.8.x)

The screencap scrubber (`src/screencap/scrubber.py`) is **post-processing only**:
- Uses **Presidio** (via `openadapt-privacy`) for text and image scrubbing
- Copies recording to `<name>-scrubbed/`, never mutates originals
- Scrubs: screenshots (PIL + Presidio Image Redactor), DB text fields, transcript.json, system_metrics.json hostname
- Does NOT scrub: video (mp4), audio, accessibility data in real-time
- No live/real-time redaction layer exists yet

## Layered Redaction Architecture (Recommended)

The research consensus is a **three-layer approach**:

### Layer 1: Pre-Capture Prevention (Data Minimisation)
- Detect `AXSecureTextField` subrole via Accessibility API — never capture password fields
- Filter known-sensitive windows (password managers, banking apps) by bundle ID
- Use ScreenCaptureKit's `SCContentFilter` to exclude specific windows/apps
- **Decision**: Cheapest and most reliable — prevents sensitive data from ever entering the pipeline

### Layer 2: Live Masking (Low-Latency, Good-Enough)
- Apply fast regex-based secret detection (API keys, tokens, SSNs) on text streams
- Use accessibility tree data for zero-cost deterministic redaction of known sensitive fields
- Delta-screening on screenshots: only OCR frames with >threshold pixel variance (90% OCR reduction)
- **Decision**: Reduces exposure window; accept some false negatives in exchange for speed

### Layer 3: Post-Processing (Current Implementation)
- Deep NER-based PII detection (Presidio, spaCy)
- Full OCR + redaction on all screenshots
- Audio transcript scrubbing
- **Decision**: Highest accuracy, acceptable latency for batch processing

## Tool Recommendations by Modality

### Text PII Detection

| Tool | Speed | Accuracy | Notes |
|------|-------|----------|-------|
| **Presidio** (current) | ~3,200 words/sec CPU (`en_core_web_lg`) | High (50+ entity types, Luhn-validated CC) | Production-proven, extensible recognizers. Current choice. |
| **DataFog** | 190x faster than Presidio | Good | Cascading regex with GLiNER fallback. Best for real-time. |
| **scrubadub** | Fast (one-liner API) | Moderate (emails, phones, SSNs, CC) | Simplest API (`scrubadub.clean(text)`); lacks structured data. Name detection needs `scrubadub_spacy` plugin. Good for prototyping only. |
| **Private AI** | Fast (proprietary DL) | Very High | Commercial, requires license. Best accuracy but cost. |
| **pii-codex** | Same as Presidio (wraps it) | Same as Presidio | Adds PII severity scoring (1-3) and policy framework mapping (HIPAA, NIST, DHS). Research-grade. |
| **Local LLMs** (Ollama) | ~50-200 tok/sec (3B) | Context-aware | Impractical as primary detector; useful as supplementary validation layer for high-stakes docs. |

**Decision guide:**
- Keep Presidio for post-processing (Layer 3) — proven, extensible, 50+ entity types
- Use `en_core_web_sm` (12MB) when only secrets detection is needed (skip NER overhead)
- Consider DataFog for live masking (Layer 2) if real-time text redaction is needed
- Private AI only if enterprise/compliance demands highest accuracy
- Local LLMs: monitor as future supplementary layer, too slow for batch processing today

### Secrets Detection (API Keys, Tokens, Passwords)

| Tool | Approach | Notes |
|------|----------|-------|
| **TruffleHog** | 800+ regex detectors | Go binary, best coverage, includes verification. **No Python API** — subprocess only. |
| **detect-secrets** (Yelp) | ~25 regex detectors + Shannon entropy | Pure Python with full library API. 4,400+ GitHub stars. Plugins: `AWSKeyDetector`, `GitHubTokenDetector`, `StripeDetector`, `SlackDetector`, `PrivateKeyDetector`, `JwtTokenDetector`, `OpenAIDetector`, `BasicAuthDetector`, `SendGridDetector`. Entropy detectors: `Base64HighEntropyString` (threshold 4.5), `HexHighEntropyString` (threshold 3.0). `KeywordDetector` catches `password = "..."` patterns. |
| **Guardrails AI** | LLM-based | Experimental, high latency |
| **Custom regex** | Targeted patterns | Fills gaps: password prompts, connection strings, spaced credit cards, JWTs, basic auth URLs |

**Decision guide:**
- Use **detect-secrets** for in-process Python integration (Layer 2 live masking) — pure Python, plugin API, `analyze_string()` works on arbitrary text without filesystem
- Use **TruffleHog** for comprehensive post-processing audit (Layer 3) — Go binary, no Python API
- **Custom regex** for remaining gaps: password-in-context detection (`password=...`, `pwd:...`), credit cards with spaces/dashes, basic auth in URLs
- Always pair credit card regex with **Luhn checksum validation** to eliminate false positives
- Secrets detection is complementary to PII detection — they catch different things

### Screen/Visual Redaction

| Tool | Speed | Notes |
|------|-------|-------|
| **ocrmac** (Vision framework) | Fast: 131ms, Accurate: 207ms | macOS-native, hardware-accelerated |
| **Tesseract** | ~300-500ms | Cross-platform, lower accuracy on macOS. Used by `presidio-image-redactor` under the hood. |
| **EasyOCR** | ~500ms+ | GPU-capable (PyTorch), 80+ languages, `batch_size` for parallel recognition |
| **PaddleOCR** | ~200ms | Good accuracy, larger dependency |

**Decision guide:**
- Use **ocrmac** on macOS — it's hardware-accelerated via Vision framework and fastest
- Use LiveText mode (174ms) for real-time, Accurate mode (207ms) for post-processing
- **Delta-screening optimization**: Track pixel variance between frames; only OCR frames with significant changes (reduces OCR calls by ~90%)
- **Frame deduplication**: Use perceptual hashing (`imagehash.dhash`, ~1-5ms/frame) to skip near-identical frames — reduces processing volume by **60-80%** in typical recordings

**Image masking approaches:**
- Presidio Image Redactor (current): convenient all-in-one (Tesseract OCR → Presidio Analyzer → PIL fill), but slower (does its own OCR internally), no parallel mode
- EasyOCR + manual bounding-box redaction: better performance at scale with GPU acceleration
- Direct OpenCV/numpy box-fill: faster if you already have OCR bounding boxes from ocrmac
- CoreGraphics/Metal rendering: fastest for GPU-accelerated rectangle drawing
- **Face blurring**: OpenCV DNN SSD face detector or MediaPipe, ~10-20ms/frame CPU, handles non-frontal faces

### Accessibility (AX) Tree Redaction

| Tool | Notes |
|------|-------|
| **macapptree** | Extracts AX tree to JSON with bounding boxes |
| **pyax** | Python AX bindings |
| **PyObjC AXUIElement** | Direct low-level access |

**Decision guide:**
- AX-based redaction is **zero ML cost** and **deterministic** — highest priority for Layer 1/2
- Detect `AXSecureTextField` subrole for instant password field identification
- Extract `AXValue` from text fields for pre-OCR text (avoids OCR entirely for native apps)
- **Limitation**: Electron/web apps have obfuscated AX trees — still need OCR fallback

**AX tree redaction strategy** (three-pronged, from Claude research):
1. **Role-based full redaction**: `AXSecureTextField` → always fully redact regardless of content
2. **Key-name sensitivity**: Regex match on key names (`password`, `token`, `secret`, `api_key`, `ssn`, `credit_card`, `cvv`, `auth`, `credential`) → auto-redact values
3. **Content-level PII**: All other `AXValue`, `AXTitle`, `AXDescription`, `AXHelp`, `AXPlaceholderValue` string values → run through unified text pipeline
- Presidio's `BatchAnalyzerEngine.analyze_dict()` handles nested dicts with dot-notation key skipping, but doesn't understand AX semantics — needs a custom `AXTreeRedactor` wrapper

### Audio/Transcript Redaction

| Tool | Notes |
|------|-------|
| **faster-whisper** | CTranslate2 inference, real-time capable, word-level timestamps |
| **whisper.cpp** | C++ port, lowest latency |
| **Silero VAD** | Voice Activity Detection, filters silence before STT |
| **webrtcvad** | Google's VAD, simpler but less accurate |

**Decision guide:**
- Use **faster-whisper** for transcription (word-level timestamps enable precise audio redaction)
- Apply VAD (Silero preferred) to skip silent segments and reduce STT load
- Audio bleeping: convert timestamp → byte offset → zero PCM samples or inject sine wave
- **Reactive delay buffer** (2-4 seconds): buffer audio, run STT+PII detection, bleep before encoding

### Video Redaction (Currently Unsupported)

**Approaches:**
1. **Frame-by-frame OCR + box overlay**: Extract frames, OCR, detect PII regions, draw filled rectangles, re-encode
2. **FFmpeg filter pipeline**: `drawbox` filter with coordinates from OCR pass
3. **Delta-based**: Only process keyframes or frames with significant pixel change

**Decision guide:**
- Video redaction is expensive — defer to post-processing only
- Consider offering "redacted screenshots + clean audio" as cheaper alternative to full video redaction
- If implementing: FFmpeg filter approach is most practical (pipe coordinates from OCR)

## Performance Optimization Strategies

### Delta-Screening (Key Optimization)
Track pixel variance between consecutive screenshots. Only run expensive OCR/NER on frames exceeding a change threshold. Research shows ~90% reduction in OCR workload for typical desktop usage.

### Multiprocessing Pipeline
```
Ingestion → Inference → Analysis → Encoding
(capture)    (OCR/STT)   (PII/NER)   (masking/output)
```
Each stage runs in a separate process with queues between them. This matches screencap's existing multiprocess architecture.

### CoreML Integration
Convert PyTorch models (spaCy, Whisper) to `.mlpackage` for Apple Neural Engine / GPU offloading. Significant speedup for on-device inference.

### Caching
- Cache OCR results per-frame hash to avoid re-processing identical screens (e.g., static documents)
- Cache NER model loading (already done by Presidio's AnalyzerEngine singleton)

## Recommended Pipeline Architecture (from Claude Research)

The optimal approach layers three detection stages from fastest/highest-precision to slowest/highest-recall, applied uniformly across all data types (screenshots → OCR, audio transcripts, AX tree snapshots):

```
Stage 1: detect-secrets plugins (fast, secrets-focused)
  → AWS keys, GitHub tokens, Stripe keys, Slack tokens,
    entropy-based secrets, keyword-matched passwords

Stage 2: Presidio + custom recognizers (NER + regex)
  → Names, emails, phones, SSNs, credit cards, locations,
    custom API key patterns, IBANs, IP addresses

Stage 3: Custom regex fallback (edge cases)
  → Password prompts, connection strings, spaced credit cards,
    JWTs, basic auth URLs
```

Key implementation details:
- Presidio's **context-aware enhancement** (`LemmaContextAwareEnhancer`): words like "aws", "key", "secret" near a regex match boost confidence, reducing false positives
- detect-secrets individual plugins expose `analyze_string()` for scanning arbitrary text without touching the filesystem
- Custom `PatternRecognizer` objects in Presidio can absorb many detect-secrets patterns over time → consolidate into single Presidio-centric pipeline
- **Faker integration** for realistic replacements via custom operator lambdas (names → fake names, emails → fake emails, etc.)
- Wrap pipeline in `concurrent.futures.ProcessPoolExecutor` or use `BatchAnalyzerEngine(n_process=4)` for batch parallelism

## Sensitivity Classes

When designing redaction rules, distinguish:

1. **Hard secrets**: API keys, tokens, passwords, private keys, connection strings — MUST be caught, regex-detectable
2. **Regulated identifiers**: SSNs, credit cards, passport numbers, medical record numbers — regex + checksum validation
3. **Personal data (PII)**: Names, emails, phone numbers, addresses — NER-based detection
4. **Organisational secrets**: Internal project names, code names, unreleased features — context-dependent, hardest to catch

**Priority**: Hard secrets > Regulated identifiers > Personal data > Organisational secrets

## Regulatory Context

- **GDPR**: Right to erasure applies; scrubbed copies must be verifiably clean
- **HIPAA**: PHI in screen recordings requires BAA-compliant handling
- **CCPA**: California residents' PII must be deletable on request
- **SOC 2**: Audit trail of what was redacted and when

## Migration Path from Current Implementation

### Phase 1: Enhance Post-Processing (Low effort, high value)
- Add secrets detection (detect-secrets) alongside Presidio PII — use `analyze_string()` plugin API for in-process integration
- Add custom `PatternRecognizer` objects to Presidio for common secret formats (AWS keys, GitHub PATs, Stripe keys, Slack tokens, JWTs)
- Add custom regex fallback for password prompts, connection strings, basic auth URLs
- Switch screenshot OCR from Presidio Image Redactor to ocrmac + direct box-fill (faster)
- Add frame deduplication via `imagehash.dhash` (~1-5ms/frame, 60-80% volume reduction)
- Add delta-screening to skip duplicate/similar screenshots

### Phase 2: Add Pre-Capture Prevention (Medium effort)
- Detect AXSecureTextField and exclude from capture
- Allow configurable app exclusion list (bundle IDs)

### Phase 3: Live Masking Layer (High effort)
- Real-time text stream scrubbing with DataFog or detect-secrets
- AX tree-based field masking during capture
- Reactive audio buffer with faster-whisper + bleeping

### Phase 4: Video Redaction (High effort)
- Frame extraction → OCR → coordinate mapping → FFmpeg drawbox → re-encode
- Or: offer "scrubbed screenshots + scrubbed audio" as lighter alternative

## Source Research

Detailed findings are in `docs/research/redaction/`:
- `chatgpt-secret-redaction-for-continuous-recording-macos-deep-research-report.md` — Broad survey of 80+ tools, two reference architectures, migration checklist
- `gemini-Python-Secrets-Redaction-Tools-macOS.md` — Real-time performance focus, specific benchmarks, system matrix
- `claude-secret-redation-on-macos.md` — Concrete three-stage pipeline architecture (detect-secrets → Presidio → custom regex), working code for each stage, AX tree redactor implementation, Presidio custom recognizer patterns, comparison matrix across all tools, frame deduplication via imagehash
