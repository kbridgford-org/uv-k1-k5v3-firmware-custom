const { test } = require('node:test');
const assert = require('node:assert/strict');
const { validateVerdict, finalize, externalId } = require('./check.cjs');

const identity = {
  run_id: '123', run_attempt: '2', pr_number: 4,
  base_sha: 'a'.repeat(40), head_sha: 'b'.repeat(40)
};
const file = 'demo/vendor/py32f071.c';
function result() {
  return {
    run_id: identity.run_id, run_attempt: identity.run_attempt,
    verdict: {
      version: 1, pr_number: identity.pr_number,
      base_sha: identity.base_sha, head_sha: identity.head_sha,
      decision: 'pass', summary: 'No new licensing violations.',
      coverage: { complete: true, files: [file] }, findings: []
    }
  };
}
const finding = {
  path: file, line: 16, license: 'GPL-2.0-or-later',
  evidence: 'This code is licensed under the GPL version 2 or later.',
  reason: 'New copyleft import.', remediation: 'Remove the imported source.'
};

test('accepts the agent decisions without independently classifying a license', () => {
  const envelope = result();
  assert.equal(validateVerdict(envelope, identity, [file]).decision, 'pass');
  envelope.verdict.decision = 'fail';
  envelope.verdict.findings = [finding];
  assert.equal(validateVerdict(envelope, identity, [file]).decision, 'fail');
  envelope.verdict.decision = 'needs_review';
  envelope.verdict.coverage = { complete: false, files: [] };
  envelope.verdict.findings = [];
  assert.equal(validateVerdict(envelope, identity, [file]).decision, 'needs_review');
});

test('rejects missing, malformed, duplicate, or unexpected result data', () => {
  for (const mutate of [
    envelope => { delete envelope.verdict; },
    envelope => { envelope.verdict.decision = 'neutral'; },
    envelope => { envelope.verdict.extra = 'ignored'; },
    envelope => { envelope.verdict.summary = ''; },
    envelope => { envelope.verdict.coverage.files.push(file); },
    envelope => { envelope.verdict.coverage.files = ['unknown.c']; }
  ]) {
    const envelope = result();
    mutate(envelope);
    assert.throws(() => validateVerdict(envelope, identity, [file]));
  }
});

test('rejects wrong PRs, stale revisions, runs, and rerun attempts', () => {
  for (const field of ['run_id', 'run_attempt', 'pr_number', 'base_sha', 'head_sha']) {
    const envelope = result();
    const target = ['run_id', 'run_attempt'].includes(field) ? envelope : envelope.verdict;
    target[field] = field === 'pr_number' ? 99 : 'c'.repeat(40);
    assert.throws(() => validateVerdict(envelope, identity, [file]), field);
  }
});

test('rejects incomplete or contradictory passing verdicts and unevidenced violations', () => {
  for (const mutate of [
    envelope => { envelope.verdict.coverage.complete = false; },
    envelope => { envelope.verdict.coverage.files = []; },
    envelope => { envelope.verdict.findings = [finding]; },
    envelope => { envelope.verdict.decision = 'fail'; }
  ]) {
    const envelope = result();
    mutate(envelope);
    assert.throws(() => validateVerdict(envelope, identity, [file]));
  }
});

test('publishes a failure rather than reusing success when agent analysis fails', async t => {
  const saved = { ...process.env };
  t.after(() => { process.env = saved; });
  Object.assign(process.env, {
    CHECK_ID: '12', GITHUB_RUN_ATTEMPT: '2', ANALYSIS_RESULT: 'failure'
  });
  const updates = [];
  const errors = [];
  const github = {
    rest: {
      checks: {
        get: async () => ({ data: {
          name: 'License policy', head_sha: identity.head_sha, external_id: externalId(identity)
        } }),
        update: async request => { updates.push(request); }
      },
      actions: { getWorkflowRun: async () => ({ data: { run_attempt: 2 } }) },
      pulls: { get: async () => ({ data: {
        state: 'open', head: { sha: identity.head_sha, repo: { full_name: 'owner/repo' } },
        base: { sha: identity.base_sha }
      } }) }
    }
  };
  await finalize({
    github,
    context: {
      repo: { owner: 'owner', repo: 'repo' }, runId: 123,
      payload: { pull_request: {
        number: 4, base: { sha: identity.base_sha }, head: { sha: identity.head_sha }
      } }
    },
    core: { error: message => errors.push(message), setFailed: message => errors.push(message) }
  });
  assert.equal(updates[0].check_run_id, 12);
  assert.equal(updates[0].conclusion, 'failure');
  assert.match(updates[0].output.summary, /did not complete successfully/);
  assert.equal(errors.length, 2);
});
