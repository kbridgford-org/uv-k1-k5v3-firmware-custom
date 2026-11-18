---
applyTo: "**"
excludeAgent: "coding-agent"
---

# Emulator evidence in code reviews

Before this review started, the `copilot-setup-steps` job in
`.github/workflows/copilot-code-review.yml` built this pull request's Fusion
firmware, booted it in the UV-K5 emulator, pressed MENU, DOWN, and EXIT, and
captured LCD screenshots. The results are in `ccr-evidence/SUMMARY.md` in the
repository root (an untracked file, not part of the diff), with raw data in
`ccr-evidence/results.json`. An identical copy is at `/tmp/ccr-evidence/`.

When reviewing a pull request:

1. Read `ccr-evidence/SUMMARY.md` before writing the review. Open it by path; it
   won't appear in the diff or in searches that skip untracked files. If it
   can't be read there, read `/tmp/ccr-evidence/SUMMARY.md`.
2. Post the evidence as a review comment. Every review must include exactly one
   comment titled `📟 Emulator evidence`, anchored to the first changed line of
   the diff (prefer a changed line under `App/ui/` or `App/app/` when the PR
   touches the UI). The comment body must contain the step table from
   `SUMMARY.md` as is, including the Markdown screenshot links, followed by the
   tested commit and the workflow run link. Don't rewrite or shorten the
   screenshot URLs. This comment is required verification output, not a style
   note, so keep it even when the review finds no other issues, and don't merge
   it into another comment. If the review overview has room for extra sections,
   also add the same table under `### 📟 Emulator evidence`.
3. If the result is FAILED, or `SUMMARY.md` is missing, say so at the top of the
   evidence comment. For a screen mismatch, compare the linked screenshots when
   available and describe the observed difference and the reported pixel count.
   If screenshots cannot be inspected, say so rather than guessing what they show.
   The comparison ignores the status bar and allows small changes in the main
   display. A mismatch warrants investigation, but does not by itself prove a
   regression or identify its cause. For build, emulator, or missing-evidence
   failures, use the logs to explain what is known; state when the cause is unknown.
   Connect a failure to changed lines only when the evidence supports it.
4. If the diff changes what the screen shows (`App/ui/`, `App/app/menu.c`,
   `App/app/main.c`, fonts, bitmaps, strings), name the matching screenshot in the
   evidence comment and note anything in the PR description it should confirm.
   If the status bar is relevant, inspect it separately from the automated check.
5. Only make claims the evidence supports. A changed or similar screenshot does
   not verify its text, every pixel, or the status bar. The emulator covers boot,
   LCD, and keypad behavior. It does not emulate RF, audio, or exact timing, so
   don't present a pass as proof of radio behavior.
6. If the tested commit in `SUMMARY.md` doesn't match the pull request head,
   note that the evidence may be stale.
