const fs = require('node:fs');

const CHECK_NAME = 'License policy';
const SHA = /^[a-f0-9]{40}$/;

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function keys(value, expected, label) {
  assert(value && typeof value === 'object' && !Array.isArray(value), `${label} must be an object`);
  assert(
    JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...expected].sort()),
    `${label} has missing or unexpected fields`
  );
}

function text(value, label, max = 4000) {
  assert(
    typeof value === 'string' && value.trim().length > 0 && value.length <= max &&
      !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value),
    `${label} must be nonempty bounded text`
  );
}

function externalId(identity) {
  return `license-policy:${identity.run_id}:${identity.run_attempt}:${identity.pr_number}:${identity.base_sha}:${identity.head_sha}`;
}

function validateVerdict(envelope, identity, changedFiles) {
  keys(envelope, ['run_id', 'run_attempt', 'verdict'], 'Result envelope');
  assert(
    envelope.run_id === identity.run_id && envelope.run_attempt === identity.run_attempt,
    'Result belongs to another workflow run or attempt'
  );
  const verdict = envelope.verdict;
  keys(verdict, [
    'version', 'pr_number', 'base_sha', 'head_sha', 'decision', 'summary', 'coverage', 'findings'
  ], 'Verdict');
  assert(verdict.version === 1, 'Unsupported verdict version');
  assert(verdict.pr_number === identity.pr_number, 'Wrong PR number');
  for (const field of ['base_sha', 'head_sha']) {
    assert(SHA.test(verdict[field]) && verdict[field] === identity[field], `Wrong ${field}`);
  }
  assert(['pass', 'fail', 'needs_review'].includes(verdict.decision), 'Invalid agent decision');
  text(verdict.summary, 'Summary');
  keys(verdict.coverage, ['complete', 'files'], 'Coverage');
  assert(typeof verdict.coverage.complete === 'boolean', 'Coverage completeness must be boolean');
  assert(
    Array.isArray(verdict.coverage.files) &&
      verdict.coverage.files.every(file => typeof file === 'string') &&
      new Set(verdict.coverage.files).size === verdict.coverage.files.length,
    'Coverage filenames must be a unique string array'
  );
  const expectedFiles = new Set(changedFiles);
  assert(verdict.coverage.files.every(file => expectedFiles.has(file)), 'Coverage contains unknown files');
  if (verdict.coverage.complete) {
    assert(verdict.coverage.files.length === expectedFiles.size, 'Complete coverage omits changed files');
  }
  assert(Array.isArray(verdict.findings) && verdict.findings.length <= 100, 'Invalid findings array');
  for (const finding of verdict.findings) {
    keys(finding, ['path', 'line', 'license', 'evidence', 'reason', 'remediation'], 'Finding');
    assert(expectedFiles.has(finding.path), 'Finding must identify a changed file');
    assert(Number.isSafeInteger(finding.line) && finding.line > 0, 'Invalid finding line');
    for (const field of ['license', 'evidence', 'reason', 'remediation']) {
      text(finding[field], `Finding ${field}`);
    }
  }
  if (verdict.decision === 'pass') {
    assert(verdict.coverage.complete, 'Incomplete reviews cannot pass');
    assert(verdict.findings.length === 0, 'Passing reviews cannot contain findings');
  }
  if (verdict.decision === 'fail') {
    assert(verdict.findings.length > 0, 'Violations require evidence');
  }
  return verdict;
}

async function finalize({ github, context, core }) {
  const pr = context.payload.pull_request;
  const identity = {
    run_id: String(context.runId),
    run_attempt: process.env.GITHUB_RUN_ATTEMPT,
    pr_number: pr.number,
    base_sha: pr.base.sha,
    head_sha: pr.head.sha
  };
  const checkId = Number(process.env.CHECK_ID);
  assert(Number.isSafeInteger(checkId) && checkId > 0, 'Invalid pending check ID');
  let conclusion;
  let title;
  let summary;
  let detail = '';
  try {
    const { data: check } = await github.rest.checks.get({
      ...context.repo, check_run_id: checkId
    });
    assert(check.name === CHECK_NAME && check.head_sha === identity.head_sha &&
      check.external_id === externalId(identity), 'Pending check identity does not match this run');
    const { data: run } = await github.rest.actions.getWorkflowRun({
      ...context.repo, run_id: context.runId
    });
    assert(String(run.run_attempt) === identity.run_attempt, 'A newer workflow attempt supersedes this result');
    const { data: current } = await github.rest.pulls.get({
      ...context.repo, pull_number: pr.number
    });
    assert(current.state === 'open' && current.head.repo?.full_name === `${context.repo.owner}/${context.repo.repo}` &&
      current.head.sha === identity.head_sha && current.base.sha === identity.base_sha,
    'PR revisions changed or PR closed during review; this result cannot approve it');
    assert(process.env.ANALYSIS_RESULT === 'success', 'Agent analysis did not complete successfully');
    assert(process.env.ARTIFACT_RESULT === 'success', 'This attempt has no readable agent verdict artifact');
    const files = await github.paginate(github.rest.pulls.listFiles, {
      ...context.repo, pull_number: pr.number, per_page: 100
    });
    assert(files.length === current.changed_files, 'GitHub returned an incomplete changed-file inventory');
    const stat = fs.lstatSync('license-result/verdict.json');
    assert(stat.isFile() && !stat.isSymbolicLink() && stat.size <= 1024 * 1024, 'Invalid or oversized verdict artifact');
    const verdict = validateVerdict(
      JSON.parse(fs.readFileSync('license-result/verdict.json', 'utf8')),
      identity, files.map(file => file.filename)
    );
    conclusion = { pass: 'success', fail: 'failure', needs_review: 'action_required' }[verdict.decision];
    title = { pass: 'Agent approved license changes', fail: 'Agent found a license policy violation',
      needs_review: 'Agent requires license review' }[verdict.decision];
    summary = verdict.summary;
    detail = verdict.findings.map(finding =>
      `${finding.path}#L${finding.line}\nLicense: ${finding.license}\nEvidence: ${finding.evidence}\n` +
      `Reason: ${finding.reason}\nRemediation: ${finding.remediation}`
    ).join('\n\n');
    assert(detail.length <= 60000, 'Finding details exceed GitHub check output limits');
  } catch (error) {
    conclusion = 'failure';
    title = 'License review could not issue approval';
    summary = error.message;
    detail = '';
    core.error(summary);
  }
  await github.rest.checks.update({
    ...context.repo,
    check_run_id: checkId,
    status: 'completed',
    conclusion,
    output: {
      title,
      summary: `${summary}\n\nReviewed head \`${identity.head_sha}\` against base \`${identity.base_sha}\`.`,
      ...(detail ? { text: detail } : {})
    }
  });
  if (conclusion !== 'success') core.setFailed(title);
}

module.exports = { externalId, validateVerdict, finalize };
