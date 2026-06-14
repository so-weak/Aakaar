// k6 load scenario: login once, then each VU repeatedly starts a workflow run
// and polls it to a terminal status.
//
//   k6 run loadtest/k6/runs.js \
//     -e AAKAAR_API=http://127.0.0.1:8000 \
//     -e AAKAAR_EMAIL=admin@tenant.example -e AAKAAR_PASSWORD=... \
//     -e AAKAAR_WORKFLOW_ID=<uuid> \
//     -e VUS=5 -e DURATION=1m
//
// The target workflow should be self-contained (no agents, no credentials);
// seed one with `python loadtest/ci/seed.py`. See loadtest/README.md for the
// rate-limiter caveat: all VUs share one client IP, so disable or raise
// AAKAAR_RATE_LIMIT_PER_MIN on the target before blaming the API for 429s.

import http from 'k6/http';
import { check, fail, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const API = (__ENV.AAKAAR_API || 'http://127.0.0.1:8000').replace(/\/$/, '');
const EMAIL = __ENV.AAKAAR_EMAIL;
const PASSWORD = __ENV.AAKAAR_PASSWORD;
const WORKFLOW_ID = __ENV.AAKAAR_WORKFLOW_ID;
const POLL_TIMEOUT_S = parseFloat(__ENV.POLL_TIMEOUT_S || '60');
const TERMINAL = ['succeeded', 'failed', 'cancelled'];

const runSucceeded = new Rate('run_succeeded');
const runDuration = new Trend('run_duration', true); // milliseconds (time metric)

export const options = {
  scenarios: {
    runs: {
      executor: 'constant-vus',
      vus: parseInt(__ENV.VUS || '5', 10),
      duration: __ENV.DURATION || '1m',
      gracefulStop: '90s', // let in-flight polls finish
    },
  },
  thresholds: {
    // API health: almost no transport/5xx failures, snappy control plane.
    http_req_failed: ['rate<0.01'],
    'http_req_duration{endpoint:start_run}': ['p(95)<2000'],
    'http_req_duration{endpoint:poll_run}': ['p(95)<1000'],
    // Workload health: runs actually finish, and finish in time.
    run_succeeded: ['rate>0.99'],
    run_duration: ['p(95)<30000'], // ms — 30s for the 4-node offline pipeline
  },
};

export function setup() {
  if (!EMAIL || !PASSWORD || !WORKFLOW_ID) {
    fail('set AAKAAR_EMAIL, AAKAAR_PASSWORD and AAKAAR_WORKFLOW_ID (see loadtest/README.md)');
  }
  const res = http.post(
    `${API}/auth/login`,
    JSON.stringify({ email: EMAIL, password: PASSWORD }),
    { headers: { 'Content-Type': 'application/json' }, tags: { endpoint: 'login' } },
  );
  if (res.status !== 200 || !res.json('access_token')) {
    fail(`login failed: ${res.status} ${res.body}`);
  }
  return { token: res.json('access_token') };
}

export default function (data) {
  const auth = {
    headers: {
      Authorization: `Bearer ${data.token}`,
      'Content-Type': 'application/json',
    },
  };

  const started = Date.now();
  const res = http.post(
    `${API}/workflows/${WORKFLOW_ID}/runs`,
    JSON.stringify({ inputs: {} }),
    Object.assign({ tags: { endpoint: 'start_run' } }, auth),
  );
  if (!check(res, { 'run started (201)': (r) => r.status === 201 })) {
    runSucceeded.add(false);
    sleep(1);
    return;
  }

  const runId = res.json('id');
  let status = res.json('status');
  const deadline = Date.now() + POLL_TIMEOUT_S * 1000;
  while (!TERMINAL.includes(status)) {
    if (Date.now() > deadline) break;
    sleep(1);
    const poll = http.get(
      `${API}/runs/${runId}`,
      Object.assign({ tags: { endpoint: 'poll_run' } }, auth),
    );
    if (poll.status === 200) {
      status = poll.json('run.status');
    }
  }

  runDuration.add(Date.now() - started);
  check({ status }, { 'run succeeded': (s) => s.status === 'succeeded' });
  runSucceeded.add(status === 'succeeded');
}
