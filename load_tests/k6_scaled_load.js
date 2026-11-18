// Scaled-down version of k6_50k_load.js: same endpoint, headers, checks and SLA thresholds, parameterised so a single machine can run it.
//   k6 run -e BASE_URL=http://127.0.0.1:8000 -e VUS=1000 -e PROFILE=blueprint|miss -e RAMP=10 -e HOLD=30 load_tests/k6_scaled_load.js
// PROFILE=blueprint : the blueprint request (same query for everyone; per-user cache => mostly cache hits after each VU's first request)
// PROFILE=miss      : a different golden-set question and a fresh user id on every iteration => ~100% cache misses (worst case, full pipeline)
// PROFILE=closed    : like miss but with NO sleep (closed-loop) -> measures the server's saturation throughput for cache-miss traffic
// PROFILE=closed_hit: the blueprint query with a fixed user per VU and NO sleep -> saturation throughput for cache-hit traffic
import http from 'k6/http';
import { check, sleep } from 'k6';

const VUS = parseInt(__ENV.VUS || '500'), RAMP = parseInt(__ENV.RAMP || '10'), HOLD = parseInt(__ENV.HOLD || '30');
const PROFILE = __ENV.PROFILE || 'blueprint';
const QUESTIONS = JSON.parse(open('../data/golden_dataset.json')).filter(g => g.answerable).map(g => g.question);

export const options = {
  stages: [{ duration: `${RAMP}s`, target: VUS }, { duration: `${HOLD}s`, target: VUS }, { duration: '3s', target: 0 }],
  thresholds: {
    'http_req_duration': ['p(50)<250', 'p(95)<450', 'p(99)<600'],   // the blueprint's 200-600 ms SLA
    'http_req_failed': ['rate<0.0005'],
    'checks': ['rate>0.999'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
  noConnectionReuse: false,
};

export default function () {
  const vuId = __VU, tenant = `tenant_${vuId % 50}`;
  let query = 'Verify batch LOT-88392 safety protocol and return triage status', user = `user_${vuId}`;
  if (PROFILE !== 'blueprint' && PROFILE !== 'closed_hit') { query = QUESTIONS[(vuId * 31 + __ITER) % QUESTIONS.length]; user = `user_${vuId}_${__ITER}`; }
  const res = http.post(`${__ENV.BASE_URL || 'http://127.0.0.1:8000'}/api/v1/agent/query`,
    JSON.stringify({ tenant_id: tenant, user_id: user, query }),
    { headers: { 'Content-Type': 'application/json', 'X-Tenant-ID': tenant }, timeout: '30s' });
  check(res, {
    'status is 200': (r) => r.status === 200,
    'latency within 200-600ms SLA': (r) => r.timings.duration <= 600,
    'no cross-tenant leak': (r) => r.status === 200 && r.json('tenant_id') === tenant,
  });
  if (PROFILE !== 'closed' && PROFILE !== 'closed_hit') sleep(1);
}
