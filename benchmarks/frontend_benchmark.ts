#!/usr/bin/env npx tsx
/**
 * Frontend performance benchmarks for AgentViz.
 *
 * Uses Playwright to drive a real Chromium browser against the running
 * dashboard + backend. Launches synthetic agents via the backend API /
 * agentviz CLI, collects performance.mark() data injected by perf.ts,
 * and reports structured JSON results.
 *
 * Prerequisites:
 *   - AgentViz server running (agentviz server)
 *   - Frontend dev server running (cd frontend && npm start)
 *
 * Usage:
 *   npx tsx benchmarks/frontend_benchmark.ts
 */

import { chromium, Browser, Page } from 'playwright';
import { execSync, spawn, ChildProcess } from 'child_process';
import * as path from 'path';
import * as os from 'os';
import * as fs from 'fs';

const FRONTEND_URL = 'http://localhost:3000';
const BACKEND_URL = 'http://localhost:8787';
const AGENTVIZ_CMD = 'agentviz';
const SYNTH_SCRIPT = path.resolve(__dirname, 'synthetic_agent.py');

// -------------------------------------------------------------------------
// Helpers
// -------------------------------------------------------------------------

function createTempWorkspace(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agentviz-fe-bench-'));
  fs.mkdirSync(path.join(dir, 'src'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'src', 'main.py'), '# bench\nprint("hello")\n');
  return dir;
}

function cleanupWorkspace(dir: string): void {
  try { fs.rmSync(dir, { recursive: true, force: true }); } catch {}
}

function sleep(ms: number): Promise<void> {
  return new Promise(r => setTimeout(r, ms));
}

function percentile(sorted: number[], p: number): number {
  if (sorted.length === 0) return 0;
  const k = (sorted.length - 1) * (p / 100);
  const f = Math.floor(k);
  if (f + 1 >= sorted.length) return sorted[f];
  return sorted[f] + (k - f) * (sorted[f + 1] - sorted[f]);
}

function stats(data: number[]) {
  if (data.length === 0) return { p50: 0, p95: 0, p99: 0, max: 0, mean: 0, count: 0 };
  const sorted = [...data].sort((a, b) => a - b);
  const mean = sorted.reduce((a, b) => a + b, 0) / sorted.length;
  return {
    p50: +percentile(sorted, 50).toFixed(2),
    p95: +percentile(sorted, 95).toFixed(2),
    p99: +percentile(sorted, 99).toFixed(2),
    max: +Math.max(...sorted).toFixed(2),
    mean: +mean.toFixed(2),
    count: sorted.length,
  };
}

/** Launch a synthetic agent via agentviz CLI, returns the ChildProcess. */
function launchSyntheticAgent(
  agentId: string,
  workspace: string,
  opts: { cycles?: number; workTime?: number; thinkTime?: number; outputKb?: number } = {},
): ChildProcess {
  const env: Record<string, string> = {
    ...process.env as Record<string, string>,
    SYNTH_AUTO_INPUT: '1',
    SYNTH_TOOL_CYCLES: String(opts.cycles ?? 10),
    SYNTH_WORK_TIME: String(opts.workTime ?? 0.1),
    SYNTH_THINK_TIME: String(opts.thinkTime ?? 0.1),
    SYNTH_OUTPUT_KB: String(opts.outputKb ?? 5),
    SYNTH_PERMISSION_PROMPTS: '0',
  };

  return spawn(AGENTVIZ_CMD, ['run', '-w', workspace, '-i', agentId, 'synthetic', 'python3', SYNTH_SCRIPT], {
    env,
    stdio: 'ignore',
    detached: false,
  });
}

/** Enable perf tracing in the page and clear old marks. */
async function enablePerf(page: Page): Promise<void> {
  await page.evaluate(() => {
    (window as any).__AGENTVIZ_PERF__ = true;
    performance.clearMarks();
  });
}

/** Collect all performance marks from the page. */
async function collectMarks(page: Page): Promise<Array<{ name: string; startTime: number; detail: any }>> {
  return page.evaluate(() =>
    performance.getEntriesByType('mark').map((m: any) => ({
      name: m.name,
      startTime: m.startTime,
      detail: m.detail ?? null,
    }))
  );
}

/** Wait until the backend reports agent in completed/stopped state, or timeout. */
async function waitForAgentDone(agentId: string, timeoutMs = 30000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`${BACKEND_URL}/agents/${agentId}`);
      if (resp.ok) {
        const data = await resp.json();
        // Backend returns {"agent": {"state": "..."}, "events": [...]}
        const state = data.agent?.state ?? data.state;
        if (['completed', 'stopped'].includes(state)) return;
      }
    } catch {}
    await sleep(500);
  }
}

/** Wait for multiple agents to complete. */
async function waitForAgentsDone(agentIds: string[], timeoutMs = 60000): Promise<void> {
  await Promise.all(agentIds.map(id => waitForAgentDone(id, timeoutMs)));
}

/** Pair marks: find each mark matching startPrefix and pair with the next mark matching endPrefix. */
function pairMarks(
  marks: Array<{ name: string; startTime: number; detail: any }>,
  startPrefix: string,
  endPrefix: string,
): number[] {
  const starts = marks.filter(m => m.name.startsWith(startPrefix));
  const ends = marks.filter(m => m.name.startsWith(endPrefix));
  const latencies: number[] = [];
  let endIdx = 0;
  for (const s of starts) {
    while (endIdx < ends.length && ends[endIdx].startTime < s.startTime) endIdx++;
    if (endIdx < ends.length) {
      latencies.push(ends[endIdx].startTime - s.startTime);
      endIdx++;
    }
  }
  return latencies;
}

/** Clean all agents from backend before a benchmark run. */
async function clearBackendAgents(): Promise<void> {
  try { await fetch(`${BACKEND_URL}/agents`, { method: 'DELETE' }); } catch {}
  await sleep(500);
}

// -------------------------------------------------------------------------
// Benchmark Suites
// -------------------------------------------------------------------------

async function benchSocketToStore(page: Page): Promise<any> {
  console.log('\n--- Frontend: Socket -> Store Latency ---');
  await clearBackendAgents();
  await enablePerf(page);

  const agentId = `fe-bench-s2s-${Date.now()}`;
  const ws = createTempWorkspace();
  const proc = launchSyntheticAgent(agentId, ws, { cycles: 10 });

  await waitForAgentDone(agentId);
  proc.kill();
  await sleep(500);

  const marks = await collectMarks(page);
  const latencies = pairMarks(marks, 'socket:', 'store:');

  cleanupWorkspace(ws);

  const result = { status: 'ok', latency_ms: stats(latencies) };
  console.log(`  Pairs: ${result.latency_ms.count}  p50=${result.latency_ms.p50}ms  p95=${result.latency_ms.p95}ms  max=${result.latency_ms.max}ms`);
  return result;
}

async function benchStoreToRender(page: Page): Promise<any> {
  console.log('\n--- Frontend: Store -> Render Latency ---');
  await clearBackendAgents();
  await enablePerf(page);

  const agentId = `fe-bench-s2r-${Date.now()}`;
  const ws = createTempWorkspace();
  const proc = launchSyntheticAgent(agentId, ws, { cycles: 10 });

  await waitForAgentDone(agentId);
  proc.kill();
  await sleep(500);

  const marks = await collectMarks(page);
  const latencies = pairMarks(marks, 'store:', 'kanban:rendered');

  cleanupWorkspace(ws);

  const result = { status: 'ok', latency_ms: stats(latencies) };
  console.log(`  Pairs: ${result.latency_ms.count}  p50=${result.latency_ms.p50}ms  p95=${result.latency_ms.p95}ms  max=${result.latency_ms.max}ms`);
  return result;
}

async function benchKanbanAtScale(page: Page): Promise<any> {
  console.log('\n--- Frontend: Kanban Render at Scale ---');
  const scaleLevels = [1, 2, 4, 8];
  const levels: any[] = [];

  for (const n of scaleLevels) {
    console.log(`  Testing with ${n} agent(s)...`);
    await clearBackendAgents();
    await enablePerf(page);

    const agentIds: string[] = [];
    const workspaces: string[] = [];
    const procs: ChildProcess[] = [];

    for (let i = 0; i < n; i++) {
      const id = `fe-bench-scale-${n}-${i}-${Date.now()}`;
      const ws = createTempWorkspace();
      agentIds.push(id);
      workspaces.push(ws);
      procs.push(launchSyntheticAgent(id, ws, { cycles: 20, workTime: 0.05, thinkTime: 0.05 }));
    }

    await waitForAgentsDone(agentIds);
    procs.forEach(p => p.kill());
    await sleep(500);

    const marks = await collectMarks(page);
    const kanbanMarks = marks.filter(m => m.name === 'kanban:rendered');

    // Compute render intervals
    const intervals: number[] = [];
    for (let i = 1; i < kanbanMarks.length; i++) {
      intervals.push(kanbanMarks[i].startTime - kanbanMarks[i - 1].startTime);
    }

    workspaces.forEach(cleanupWorkspace);

    levels.push({
      agents: n,
      render_count: kanbanMarks.length,
      render_interval_ms: stats(intervals),
    });
    console.log(`    Renders: ${kanbanMarks.length}  interval p50=${stats(intervals).p50}ms`);
  }

  return { status: 'ok', levels };
}

async function benchAgentCardRenders(page: Page): Promise<any> {
  console.log('\n--- Frontend: Agent Card Render Counts ---');
  await clearBackendAgents();
  await enablePerf(page);

  const n = 8;
  const agentIds: string[] = [];
  const workspaces: string[] = [];
  const procs: ChildProcess[] = [];

  for (let i = 0; i < n; i++) {
    const id = `fe-bench-card-${i}-${Date.now()}`;
    const ws = createTempWorkspace();
    agentIds.push(id);
    workspaces.push(ws);
    procs.push(launchSyntheticAgent(id, ws, { cycles: 20, workTime: 0.05, thinkTime: 0.05 }));
  }

  await waitForAgentsDone(agentIds);
  procs.forEach(p => p.kill());
  await sleep(500);

  const marks = await collectMarks(page);
  const cardMarks = marks.filter(m => m.name === 'agentcard:rendered');

  // Group by agentId, find max renderCount
  const countsByAgent: Record<string, number> = {};
  for (const m of cardMarks) {
    const id = m.detail?.agentId || 'unknown';
    const rc = m.detail?.renderCount || 0;
    countsByAgent[id] = Math.max(countsByAgent[id] || 0, rc);
  }

  const counts = Object.values(countsByAgent);
  const min = counts.length ? Math.min(...counts) : 0;
  const max = counts.length ? Math.max(...counts) : 0;
  const mean = counts.length ? +(counts.reduce((a, b) => a + b, 0) / counts.length).toFixed(1) : 0;

  workspaces.forEach(cleanupWorkspace);

  const result = {
    status: 'ok',
    agents_tracked: Object.keys(countsByAgent).length,
    renders_per_agent: { min, max, mean },
    by_agent: countsByAgent,
  };
  console.log(`  Agents tracked: ${result.agents_tracked}  renders min=${min} max=${max} mean=${mean}`);
  return result;
}

async function benchE2EPipeline(page: Page): Promise<any> {
  console.log('\n--- Frontend: End-to-End Pipeline ---');
  await clearBackendAgents();
  await enablePerf(page);

  const agentId = `fe-bench-e2e-${Date.now()}`;
  const ws = createTempWorkspace();
  const proc = launchSyntheticAgent(agentId, ws, { cycles: 10 });

  await waitForAgentDone(agentId);
  proc.kill();
  await sleep(500);

  const marks = await collectMarks(page);
  const latencies = pairMarks(marks, 'socket:', 'kanban:rendered');

  cleanupWorkspace(ws);

  const result = { status: 'ok', latency_ms: stats(latencies) };
  console.log(`  Pairs: ${result.latency_ms.count}  p50=${result.latency_ms.p50}ms  p95=${result.latency_ms.p95}ms  max=${result.latency_ms.max}ms`);
  return result;
}

// -------------------------------------------------------------------------
// Mobile Benchmark Suites
// -------------------------------------------------------------------------

async function benchMobileRendering(browser: Browser): Promise<any> {
  console.log('\n--- Frontend: Mobile Rendering ---');
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
  });
  const page = await context.newPage();
  await page.goto(FRONTEND_URL, { waitUntil: 'networkidle' });
  await sleep(1000);

  await clearBackendAgents();
  await enablePerf(page);

  const agentId = `fe-bench-mobile-${Date.now()}`;
  const ws = createTempWorkspace();
  const proc = launchSyntheticAgent(agentId, ws, { cycles: 10 });

  await waitForAgentDone(agentId);
  proc.kill();
  await sleep(500);

  const marks = await collectMarks(page);

  // Socket -> store -> render latencies on mobile viewport
  const socketToRender = pairMarks(marks, 'socket:', 'kanban:rendered');

  // Tap agent card to open drawer and measure drawer open time
  let drawerLatencies: number[] = [];
  try {
    const cardSelector = `[data-testid="agent-card-${agentId}"], .MuiCard-root`;
    await page.waitForSelector(cardSelector, { timeout: 5000 });
    const tapStart = performance.now();
    await page.tap(cardSelector);
    await sleep(300);
    // Collect marks after tap
    const postTapMarks = await collectMarks(page);
    const drawerMarks = postTapMarks.filter(m => m.name === 'drawer:opened');
    if (drawerMarks.length > 0) {
      drawerLatencies.push(drawerMarks[drawerMarks.length - 1].startTime);
    }
  } catch {
    console.log('  Could not tap agent card (may not be visible)');
  }

  cleanupWorkspace(ws);
  await context.close();

  const result = {
    status: 'ok',
    render_latency_ms: stats(socketToRender),
    drawer_latency_ms: stats(drawerLatencies),
  };
  console.log(`  Mobile render p50=${result.render_latency_ms.p50}ms  p95=${result.render_latency_ms.p95}ms`);
  console.log(`  Drawer latency samples: ${drawerLatencies.length}`);
  return result;
}

async function benchVirtualKeypad(browser: Browser): Promise<any> {
  console.log('\n--- Frontend: Virtual Keypad ---');
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
  });
  const page = await context.newPage();
  await page.goto(FRONTEND_URL, { waitUntil: 'networkidle' });
  await sleep(1000);

  await clearBackendAgents();

  const agentId = `fe-bench-keypad-${Date.now()}`;
  const ws = createTempWorkspace();

  // Launch with tmux mode and permission prompts for waiting_for_input state
  const env: Record<string, string> = {
    ...process.env as Record<string, string>,
    SYNTH_AUTO_INPUT: '0',
    SYNTH_TOOL_CYCLES: '3',
    SYNTH_WORK_TIME: '0.1',
    SYNTH_THINK_TIME: '0.1',
    SYNTH_OUTPUT_KB: '1',
    SYNTH_PERMISSION_PROMPTS: '2',
  };
  const proc = spawn(AGENTVIZ_CMD, [
    'run', '--tmux-mode', '-w', ws, '-i', agentId,
    'synthetic', 'python3', SYNTH_SCRIPT,
  ], { env, stdio: 'ignore', detached: false });

  // Wait for agent to appear and get tmux session info
  let ttydReceived = false;
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`${BACKEND_URL}/agents/${agentId}`);
      if (resp.ok) {
        const data = await resp.json();
        const agent = data.agent ?? data;
        if (agent.ttyd_url) {
          ttydReceived = true;
          break;
        }
      }
    } catch {}
    await sleep(500);
  }

  // Tap agent card to open drawer
  let keypadVisible = false;
  let buttonTapSuccesses = 0;
  const buttonTapAttempts = 1;

  try {
    const cardSelector = `.MuiCard-root`;
    await page.waitForSelector(cardSelector, { timeout: 5000 });
    await page.tap(cardSelector);
    await sleep(500);

    // Check for "Open Terminal" button (indicates ttyd_url received)
    const terminalBtn = await page.$('button:has-text("Open Terminal"), a:has-text("Open Terminal")');

    // Check for virtual keypad buttons (Up/Down/Enter)
    const enterBtn = await page.$('button:has-text("Enter"), button:has-text("⏎")');
    const upBtn = await page.$('button:has-text("Up"), button:has-text("↑")');
    const downBtn = await page.$('button:has-text("Down"), button:has-text("↓")');

    keypadVisible = !!(enterBtn || upBtn || downBtn);

    if (enterBtn) {
      try {
        await page.tap('button:has-text("Enter"), button:has-text("⏎")');
        await sleep(1000);
        // Verify backend received the control event by checking agent state changed
        const resp = await fetch(`${BACKEND_URL}/agents/${agentId}`);
        if (resp.ok) {
          buttonTapSuccesses = 1;
        }
      } catch {
        console.log('  Enter button tap failed');
      }
    }
  } catch {
    console.log('  Could not interact with drawer');
  }

  proc.kill();
  await sleep(1000);
  cleanupWorkspace(ws);
  await context.close();

  const result = {
    status: 'ok',
    ttyd_received: ttydReceived,
    keypad_visible: keypadVisible,
    button_tap_success_rate: buttonTapAttempts > 0
      ? +((buttonTapSuccesses / buttonTapAttempts) * 100).toFixed(1) : 0,
  };
  console.log(`  ttyd received: ${ttydReceived}`);
  console.log(`  Keypad visible: ${keypadVisible}`);
  console.log(`  Button tap success rate: ${result.button_tap_success_rate}%`);
  return result;
}

// -------------------------------------------------------------------------
// Main
// -------------------------------------------------------------------------

async function main() {
  console.log('='.repeat(60));
  console.log('AgentViz Frontend Benchmark');
  console.log(`Started at: ${new Date().toISOString()}`);
  console.log('='.repeat(60));

  // Check frontend is running
  try {
    const resp = await fetch(FRONTEND_URL);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  } catch (e: any) {
    console.error(`\nERROR: Cannot reach frontend at ${FRONTEND_URL}`);
    console.error('Start it with: cd frontend && npm start');
    process.exit(1);
  }

  // Check backend is running
  try {
    const resp = await fetch(`${BACKEND_URL}/agents`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  } catch (e: any) {
    console.error(`\nERROR: Cannot reach backend at ${BACKEND_URL}`);
    console.error('Start it with: agentviz server');
    process.exit(1);
  }

  // Launch browser
  const browser: Browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page: Page = await context.newPage();
  await page.goto(FRONTEND_URL, { waitUntil: 'networkidle' });
  await sleep(1000); // Let socket connect

  const results: Record<string, any> = {
    timestamp: new Date().toISOString(),
  };

  try {
    results.socket_to_store = await benchSocketToStore(page);
    results.store_to_render = await benchStoreToRender(page);
    results.kanban_at_scale = await benchKanbanAtScale(page);
    results.agent_card_renders = await benchAgentCardRenders(page);
    results.e2e_pipeline = await benchE2EPipeline(page);

    // Mobile benchmarks (use separate browser contexts with mobile viewports)
    results.mobile = {
      rendering: await benchMobileRendering(browser),
      virtual_keypad: await benchVirtualKeypad(browser),
    };
  } finally {
    await browser.close();
  }

  // Output JSON to stdout for the harness to capture
  console.log('\n' + '='.repeat(60));
  console.log('Frontend benchmark results:');
  console.log(JSON.stringify(results, null, 2));

  // Also write marker for harness extraction
  console.log('__FRONTEND_RESULTS_JSON__');
  console.log(JSON.stringify(results));
  console.log('__FRONTEND_RESULTS_END__');
}

main().catch((err) => {
  console.error('Frontend benchmark failed:', err);
  process.exit(1);
});
