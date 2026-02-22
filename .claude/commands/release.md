Create a new release by bumping the version and pushing a tag to trigger the GH Actions release workflow.

Takes an optional argument for the version number (e.g. `0.3.0`). If not provided, ask the user what version to release.

Steps:
1. Run `git tag --sort=-v:refname | head -1` to show the current latest tag.
2. Run `git log <latest-tag>..HEAD --oneline` to show commits since the last release.
3. If no version was provided as an argument, ask the user what version number to use.
4. Bump the `version` field in `pyproject.toml` to the new version.
5. Commit the version bump with message `bump version to <version>`.
6. Create a git tag `v<version>`.
7. Ask the user to confirm before pushing.
8. Push the commit and tag: `git push origin main && git push origin v<version>`.
9. Report that the release workflow has been triggered.

Important:
- The version argument should NOT include the `v` prefix (e.g. `0.3.0` not `v0.3.0`). Strip it if provided.
- Do not add any Claude co-authoring attribution to the commit.
- If there are no new commits since the last tag, warn the user and ask if they still want to proceed.
