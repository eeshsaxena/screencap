Create a new release by bumping the version and pushing a tag to trigger the GH Actions release workflow.

Takes an optional argument for the version number (e.g. `0.3.0`). If not provided, ask the user what version to release.

Steps:
1. Run `git tag --sort=-v:refname | head -1` to show the current latest tag.
2. Run `git log <latest-tag>..HEAD --oneline` to show commits since the last release.
3. If no version was provided as an argument, ask the user what version number to use.
4. Generate changelog entries for the new version:
   a. Reset any pending changelog changes: `git checkout -- CHANGELOG.md` (ignore errors if file doesn't exist).
   b. Collect commits: `git log --no-merges --format='%s' <latest-tag>..HEAD`.
   c. Filter out commits starting with `bump version`.
   d. Parse each commit as a conventional commit (`type[(scope)]: description`). Group into categories:
      - `feat` → **Added**
      - `fix` → **Fixed**
      - `refactor`, `perf`, `style` → **Changed**
      - `test`, `chore`, `docs`, `build`, `ci` → **excluded** (do not include)
      - Non-conventional commits → **Other**
   e. Format each entry: strip the `type(scope):` prefix. If the commit had a scope, keep it as a bold prefix (e.g. `fix(viewer): cap events` → `- **viewer:** Cap events`). Capitalize the first letter of the description.
   f. Build a version section header: `## [<version>] - <today's date as YYYY-MM-DD>`.
   g. Under the header, add subsections (`### Added`, `### Fixed`, `### Changed`, `### Other`) — only include subsections that have entries. Omit empty categories entirely.
   h. If `CHANGELOG.md` exists, prepend the new section after the Keep a Changelog header (after the blank line following the `and this project adheres to` line). If `CHANGELOG.md` does not exist, create it with this header first:
      ```
      # Changelog

      All notable changes to this project will be documented in this file.

      The format is based on [Keep a Changelog](https://keepachangelog.com/),
      and this project adheres to [Semantic Versioning](https://semver.org/).
      ```
   i. Show the generated changelog section to the user for review before proceeding.
5. Bump the `version` field in `pyproject.toml` to the new version.
6. Stage both `pyproject.toml` and `CHANGELOG.md`, then commit with message `bump version to <version>`.
7. Create a git tag `v<version>`.
8. Ask the user to confirm before pushing.
9. Push the commit and tag: `git push origin main && git push origin v<version>`.
10. Report that the release workflow has been triggered and that a GitHub Release will be created with the changelog as release notes.

Important:
- The version argument should NOT include the `v` prefix (e.g. `0.3.0` not `v0.3.0`). Strip it if provided.
- Do not add any Claude co-authoring attribution to the commit.
- If there are no new commits since the last tag, warn the user and ask if they still want to proceed.
- Version numbers in CHANGELOG.md headers should NOT have a `v` prefix (e.g. `## [0.9.0]` not `## [v0.9.0]`).
