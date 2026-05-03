const DATA_URL = 'data/news_s2.json';
const CYCLES_URL = 'data/cycles.json';
const REFRESH_MS = 5 * 60 * 1000;

const state = {
  data: null,
  cycles: [],
  selectedTopic: null,
  selectedCycle: null,
  scope: localStorage.getItem('dream-news-scope') || 'current',
  view: 'retention',
  sort: localStorage.getItem('dream-news-sort') || 'horizon',
  signal: localStorage.getItem('dream-news-signal') || 'corrected',
  theme: localStorage.getItem('dream-news-theme') || 'dark',
  chart: null,
  chartMaxed: false,
  chartToolsVisible: false,
  lastDataStamp: null
};

const els = {
  statusDot: document.getElementById('status-dot'),
  lastUpdated: document.getElementById('last-updated'),
  statTopics: document.getElementById('stat-topics'),
  statArticles: document.getElementById('stat-articles'),
  statSources: document.getElementById('stat-sources'),
  statErrors: document.getElementById('stat-errors'),
  topicSelect: document.getElementById('topic-select'),
  viewSelect: document.getElementById('view-select'),
  signalSelect: document.getElementById('signal-select'),
  sortSelect: document.getElementById('sort-select'),
  scopeSelect: document.getElementById('scope-select'),
  themeBtn: document.getElementById('theme-btn'),
  refreshBtn: document.getElementById('refresh-btn'),
  chartTitle: document.getElementById('chart-title'),
  phaseBadge: document.getElementById('phase-badge'),
  chartMaxBtn: document.getElementById('chart-max-btn'),
  chart: document.getElementById('chart'),
  chartNote: document.getElementById('chart-note'),
  chartLegend: document.getElementById('chart-legend'),
  metricsStrip: document.getElementById('metrics-strip'),
  topicTable: document.querySelector('#topic-table tbody'),
  topicBoard: document.getElementById('topic-board'),
  boardTitle: document.getElementById('board-title'),
  tableTitle: document.getElementById('table-title'),
  storyTitle: document.getElementById('story-title'),
  storyList: document.getElementById('story-list'),
  storyCount: document.getElementById('story-count'),
  storyTemplate: document.getElementById('story-template'),
  sourceList: document.getElementById('source-list'),
  sourceCount: document.getElementById('source-count')
};

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function chartColors() {
  return {
    text: cssVar('--text') || '#edf4ff',
    muted: cssVar('--muted') || '#9aa8c3',
    line: cssVar('--line') || 'rgba(255,255,255,.12)',
    accent: cssVar('--accent') || '#7dd3fc',
    accent2: cssVar('--accent-2') || '#a78bfa',
    warn: cssVar('--warn') || '#fde68a',
    bad: cssVar('--bad') || '#fda4af',
    good: cssVar('--good') || '#86efac'
  };
}

function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
  els.themeBtn.textContent = `Theme: ${state.theme === 'dark' ? 'Dark' : 'Light'}`;
  localStorage.setItem('dream-news-theme', state.theme);
}

function fmtNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });
}

function fmtHours(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  if (value >= 48) return `${fmtNumber(value / 24, 1)}d`;
  return `${fmtNumber(value, 1)}h`;
}

function fmtDate(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function shortDate(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '-';
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit' });
}

function ageLabel(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  const diff = Date.now() - d.getTime();
  if (Number.isNaN(diff)) return '-';
  const mins = Math.max(0, Math.round(diff / 60000));
  if (mins < 90) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return `${Math.round(Number(value) * 100)}%`;
}

function retention(x, tau, beta) {
  if (!tau || !beta) return null;
  return Math.exp(-Math.pow(Math.max(0, x) / tau, beta));
}

function halfLife(tau, beta) {
  if (!tau || !beta) return null;
  return tau * Math.pow(Math.log(2), 1 / beta);
}

function clampSignal(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return null;
  return Math.max(0, Math.min(1.5, Number(v)));
}

function observedValue(point, mode = state.signal) {
  if (!point) return null;
  const preferred = mode === 'raw' ? point.observed_raw : point.observed_corrected;
  const fallback = mode === 'raw' ? point.observed : (point.observed_corrected ?? point.observed);
  return clampSignal(preferred ?? fallback);
}

function fitValue(point, topic) {
  const tau = topic?.fit?.tau_hours;
  const beta = topic?.fit?.beta;
  return retention(point?.x_hours || 0, tau, beta) ?? clampSignal(point?.fit) ?? 0;
}

function topicDust(topic) {
  const values = topic?.series || [];
  const late = values.slice(Math.floor(values.length / 2)).filter(d => observedValue(d) !== null);
  if (!late.length) return topic?.residual_dust;
  const rms = Math.sqrt(late.reduce((s, d) => s + Math.pow(observedValue(d) - fitValue(d, topic), 2), 0) / late.length);
  return rms;
}

function topicNewest(topic) {
  if (state.scope === 'cycles') return new Date(topic.ended_at || topic.archived_at || topic.peak_at || 0).getTime();
  const articles = state.data?.articles || [];
  const matches = articles.filter(a => a.topic === topic.key);
  if (!matches.length) return null;
  return matches.map(a => new Date(a.published_at).getTime()).filter(Number.isFinite).sort((a,b) => b - a)[0];
}

function cycleToTopic(cycle) {
  const fit = cycle.fit || {};
  return {
    key: cycle.cycle_id,
    cycle_id: cycle.cycle_id,
    topic_key: cycle.topic,
    label: cycle.topic_label || cycle.topic || 'Archived cycle',
    article_count: cycle.article_count || 0,
    phase: cycle.verdict || cycle.phase || 'Archived cycle',
    event_horizon: cycle.event_horizon || {},
    residual_dust: fit.residual_dust ?? cycle.residual_dust,
    circadian_bias: cycle.circadian_bias,
    peak_bin_index: cycle.peak_bin_index,
    peak_at: cycle.peaked_at || cycle.peak_at,
    started_at: cycle.started_at,
    ended_at: cycle.ended_at,
    archived_at: cycle.archived_at,
    fit: {
      tau_hours: fit.tau_hours,
      beta: fit.beta,
      half_life_hours: fit.half_life_hours,
      log_r2: fit.log_r2,
      delta_aic_vs_exp: fit.delta_aic_vs_exp,
      coherence_left_hours: fit.coherence_left_hours,
      fit_status: 'formal',
      fit_reason: cycle.fit_reason || 'Archived completed cycle'
    },
    series: cycle.series || [],
    sticky_stories: cycle.sticky_stories || [],
    max_stickiness: cycle.max_stickiness || 0,
    isArchivedCycle: true
  };
}

function getCyclesSorted() {
  const cycles = [...(state.cycles || [])];
  cycles.sort((a, b) => {
    if (state.sort === 'horizon') return ((b.event_horizon || {}).max_score || (b.event_horizon || {}).score || 0) - ((a.event_horizon || {}).max_score || (a.event_horizon || {}).score || 0);
    if (state.sort === 'stickiness') return (b.max_stickiness || 0) - (a.max_stickiness || 0);
    if (state.sort === 'lambda') return (b.fit?.tau_hours || 0) - (a.fit?.tau_hours || 0);
    if (state.sort === 'dust') return ((b.fit?.residual_dust ?? b.residual_dust) || 0) - ((a.fit?.residual_dust ?? a.residual_dust) || 0);
    if (state.sort === 'fit') return (b.fit?.log_r2 || -10) - (a.fit?.log_r2 || -10);
    if (state.sort === 'recent') return new Date(b.peaked_at || b.archived_at || 0) - new Date(a.peaked_at || a.archived_at || 0);
    return (b.article_count || 0) - (a.article_count || 0);
  });
  return cycles;
}

function getTopicsSorted() {
  if (state.scope === 'cycles') return getCyclesSorted().map(cycleToTopic);
  const topics = [...(state.data?.topics || [])];
  topics.sort((a, b) => {
    if (state.sort === 'horizon') return topicHorizon(b) - topicHorizon(a);
    if (state.sort === 'stickiness') return topicStickiness(b) - topicStickiness(a);
    if (state.sort === 'lambda') return (b.fit?.tau_hours || 0) - (a.fit?.tau_hours || 0);
    if (state.sort === 'dust') return (topicDust(b) || 0) - (topicDust(a) || 0);
    if (state.sort === 'fit') return (b.fit?.log_r2 || -10) - (a.fit?.log_r2 || -10);
    if (state.sort === 'recent') return (topicNewest(b) || 0) - (topicNewest(a) || 0);
    return (b.article_count || 0) - (a.article_count || 0);
  });
  return topics;
}

function selectedTopic() {
  const topics = getTopicsSorted();
  if (!topics.length) return null;
  const key = state.scope === 'cycles' ? state.selectedCycle : state.selectedTopic;
  return topics.find(t => t.key === key) || topics[0];
}

function articleXHours(article, topic) {
  if (!article?.published_at) return null;
  const published = new Date(article.published_at).getTime();
  let peak = topic?.peak_at ? new Date(topic.peak_at).getTime() : NaN;
  if (!Number.isFinite(peak) && topic?.peak_bin_index != null && state.data?.generated_at) {
    const generated = new Date(state.data.generated_at).getTime();
    const windowHours = state.data.window_hours || 168;
    const binHours = state.data.bin_hours || 3;
    peak = generated - windowHours * 3600 * 1000 + topic.peak_bin_index * binHours * 3600 * 1000;
  }
  if (!Number.isFinite(published) || !Number.isFinite(peak)) return null;
  return Math.max(0, (published - peak) / 3600000);
}

function nearestSeriesPoint(topic, xHours) {
  const series = topic?.series || [];
  if (!series.length || xHours == null) return null;
  return series.reduce((best, point) => {
    if (!best) return point;
    return Math.abs(Number(point.x_hours) - xHours) < Math.abs(Number(best.x_hours) - xHours) ? point : best;
  }, null);
}

function storyStickiness(article, topic) {
  if (article?.stickiness_score != null) {
    return {
      score: Number(article.stickiness_score) || 0,
      residual: article.residual_contribution ?? article.residual ?? null,
      expected: article.expected_s2 ?? null,
      xHours: article.age_after_peak_hours ?? article.x_hours ?? null,
      postLambda: Boolean(article.post_lambda_q),
      role: article.role || 'archived survivor',
      horizonScore: article.horizon_score ?? null,
      horizonPhase: article.horizon_phase ?? null,
      horizonIndex: article.horizon_index ?? null
    };
  }
  const fit = topic?.fit || {};
  const formal = fit.fit_status === 'formal';
  const xHours = articleXHours(article, topic);
  if (!formal) {
    return { score: 0, residual: null, expected: null, xHours, postLambda: false, role: 'latest activity', horizonScore: article.horizon_score ?? null, horizonPhase: article.horizon_phase ?? null, horizonIndex: article.horizon_index ?? null };
  }
  const point = nearestSeriesPoint(topic, xHours);
  const expected = point ? fitValue(point, topic) : null;
  const observed = point ? observedValue(point) : null;
  const residual = observed == null || expected == null ? 0 : Math.max(0, observed - expected);
  const tau = Number(fit.tau_hours) || 0;
  const postLambda = tau > 0 && xHours != null && xHours >= tau;
  const ageWeight = tau > 0 && xHours != null ? 0.45 + 0.55 * Math.min(1, xHours / tau) : 0.45;
  const postBonus = postLambda && residual > 0 ? 0.18 : 0;
  const score = Math.round(100 * Math.min(1, residual * 2.2 * ageWeight + postBonus));
  let role = 'decays with baseline';
  if (score > 0 && postLambda) role = 'post-lambda_q survivor';
  else if (score > 0) role = 'positive S2 residual';
  return { score, residual, expected, xHours, postLambda, role, horizonScore: article.horizon_score ?? null, horizonPhase: article.horizon_phase ?? null, horizonIndex: article.horizon_index ?? null };
}

function topicStickiness(topic) {
  if (topic?.max_stickiness != null) return Number(topic.max_stickiness) || 0;
  const articles = state.data?.articles || [];
  const scores = articles
    .filter(article => article.topic === topic.key)
    .map(article => storyStickiness(article, topic).score)
    .sort((a, b) => b - a);
  if (!scores.length) return 0;
  const top = scores.slice(0, 5);
  return top.reduce((sum, value) => sum + value, 0) / top.length;
}

function topicHorizon(topic) {
  const h = topic?.event_horizon || {};
  return Number(h.score ?? h.max_score ?? 0) || 0;
}

function topicHorizonLabel(topic) {
  const h = topic?.event_horizon || {};
  const score = Number(h.score ?? h.max_score ?? 0) || 0;
  const phase = h.phase || 'warming up';
  return `H ${Math.round(score)} · ${phase}`;
}

function tailReadiness(topic) {
  const fit = topic.fit || {};
  if (fit.fit_status === 'formal') return { percent: 100, needText: 'formal S2', label: 'formal S2' };
  const binHours = state.data?.bin_hours || 3;
  const minBins = 4;
  const minArticles = 2;
  const minSpanHours = (minBins - 1) * binHours;
  const series = topic.series || [];
  const observedTail = series.filter(d => Number(d.x_hours) >= 0).filter(d => observedValue(d) !== null);
  const nonzeroBins = observedTail.filter(d => Number(observedValue(d)) > 0.001).length;
  const tailSpanHours = observedTail.length ? Math.max(...observedTail.map(d => Number(d.x_hours) || 0)) : 0;
  const articleCount = topic.article_count || 0;
  const score = Math.min(
    Math.min(1, nonzeroBins / minBins),
    Math.min(1, articleCount / minArticles),
    Math.min(1, tailSpanHours / minSpanHours)
  );
  const percent = Math.max(0, Math.min(100, Math.round(score * 100)));
  const needBins = Math.max(0, minBins - nonzeroBins);
  const needArticles = Math.max(0, minArticles - articleCount);
  const needHours = Math.max(0, Math.ceil(minSpanHours - tailSpanHours));
  const needs = [];
  if (needBins > 0) needs.push(`+${needBins} bins`);
  if (needArticles > 0) needs.push(`+${needArticles} articles`);
  if (needHours > 0) needs.push(`+${needHours}h span`);
  return {
    percent,
    nonzeroBins,
    tailSpanHours,
    needText: needs.length ? `needs ${needs.join(' / ')}` : 'ready for formal fit',
    label: `${percent}% tail ready`
  };
}

async function fetchJsonOrDefault(url, fallback) {
  try {
    const response = await fetch(`${url}?t=${Date.now()}`, { cache: 'no-store' });
    if (!response.ok) return fallback;
    return await response.json();
  } catch (_) {
    return fallback;
  }
}

async function loadData({ quiet = false } = {}) {
  try {
    if (!quiet) els.lastUpdated.textContent = 'Loading live JSON...';
    const nextData = await fetchJsonOrDefault(DATA_URL, null);
    if (!nextData) throw new Error('news_s2.json unavailable');
    const nextCycles = await fetchJsonOrDefault(CYCLES_URL, []);
    const cycles = Array.isArray(nextCycles) ? nextCycles : (nextCycles.cycles || []);
    const lastCycle = cycles[0]?.archived_at || cycles[0]?.cycle_id || '';
    const nextStamp = `${nextData.generated_at || nextData.updated_at || JSON.stringify(nextData.summary || {})}|${cycles.length}|${lastCycle}`;
    if (quiet && nextStamp === state.lastDataStamp) return;
    state.lastDataStamp = nextStamp;
    state.data = nextData;
    state.cycles = cycles;
    const topics = state.data.topics || [];
    if (!state.selectedTopic || !topics.some(t => t.key === state.selectedTopic)) state.selectedTopic = topics[0]?.key || null;
    if (!state.selectedCycle || !state.cycles.some(c => c.cycle_id === state.selectedCycle)) state.selectedCycle = state.cycles[0]?.cycle_id || null;
    els.statusDot.className = 'status-dot ok';
    render();
  } catch (err) {
    els.statusDot.className = 'status-dot err';
    els.lastUpdated.textContent = `Data load failed: ${err.message}`;
  }
}

function render() {
  if (!state.data) return;
  const errors = state.data.summary?.fetch_errors || [];
  els.lastUpdated.textContent = `updated ${fmtDate(state.data.generated_at)}`;
  els.statTopics.textContent = (state.data.topics || []).length;
  els.statArticles.textContent = state.data.summary?.article_count ?? '-';
  els.statSources.textContent = state.data.summary?.source_count ?? '-';
  els.statErrors.textContent = errors.length;
  renderTopicOptions();
  renderSelected();
  renderTopicBoard();
  renderTopicTable();
  renderStories();
  renderSources();
}

function renderTopicOptions() {
  const topics = getTopicsSorted();
  els.topicSelect.innerHTML = '';
  topics.forEach(topic => {
    const opt = document.createElement('option');
    opt.value = topic.key;
    opt.textContent = state.scope === 'cycles'
      ? `${topic.label} · peak ${shortDate(topic.peak_at)} · ${topic.phase}`
      : `${topic.label} (${topic.article_count})`;
    els.topicSelect.appendChild(opt);
  });
  const fallback = topics[0]?.key || '';
  if (state.scope === 'cycles') {
    els.topicSelect.value = state.selectedCycle || fallback;
  } else {
    els.topicSelect.value = state.selectedTopic || fallback;
  }
  els.viewSelect.value = state.view;
  els.signalSelect.value = state.signal;
  els.sortSelect.value = state.sort;
  els.scopeSelect.value = state.scope;
}

function renderSelected() {
  const topic = selectedTopic();
  if (!topic) {
    els.phaseBadge.textContent = 'No cycles';
    els.metricsStrip.innerHTML = '<div class="empty-state metric-empty">No selected signal yet.</div>';
    els.chartTitle.textContent = state.scope === 'cycles' ? 'Cycle archive initializing' : 'Retention curve';
    if (state.chart) {
      state.chart.dispose();
      state.chart = null;
    }
    els.chart.innerHTML = '<div class="empty-state">No data for this scope yet.</div>';
    els.chartNote.textContent = state.scope === 'cycles' ? 'Completed formal cycles will appear after a prior formal topic state is archived by the next update run.' : 'Waiting for live feed history.';
    els.chartLegend.innerHTML = '';
    return;
  }
  if (state.scope === 'cycles') state.selectedCycle = topic.key;
  else state.selectedTopic = topic.key;
  const fit = topic.fit || {};
  const bias = topic.circadian_bias;
  els.phaseBadge.textContent = state.scope === 'cycles' ? (topic.phase || 'Archived cycle') : (topic.phase || '-');
  els.metricsStrip.innerHTML = [
    ['lambda_q / tau', fmtHours(fit.tau_hours), 'coherence cliff'],
    ['D_eff / beta', fmtNumber(fit.beta, 2), (fit.beta || 0) > 1 ? 'cliff-like' : 'long tail'],
    ['Half-life', fmtHours(halfLife(fit.tau_hours, fit.beta)), 'R(lambda)=0.5'],
    ['log-R2', fmtNumber(fit.log_r2, 3), 'fit quality'],
    ['Delta AIC', fmtNumber(fit.delta_aic_vs_exp, 2), 'vs D=1 exp'],
    ['Horizon', fmtNumber((topic.event_horizon || {}).score ?? (topic.event_horizon || {}).max_score ?? 0, 0), (topic.event_horizon || {}).phase || 'S2 bound']
  ].map(([k, v, s]) => `<div class="metric"><span>${k} <em>· ${s}</em></span><strong>${v}</strong></div>`).join('');

  const suffix = state.signal === 'corrected' ? 'circadian-corrected' : 'raw feed';
  const prefix = state.scope === 'cycles' ? `${topic.label}: archived` : topic.label;
  if (state.view === 'residuals') els.chartTitle.textContent = `${prefix} residual dust (${suffix})`;
  else if (state.view === 'comparison') els.chartTitle.textContent = `${prefix} S2 vs exponential (${suffix})`;
  else if (state.view === 'horizon') els.chartTitle.textContent = `${prefix} S2 event horizon (${suffix})`;
  else els.chartTitle.textContent = `${prefix} S2 retention (${suffix})`;
  drawInteractiveChart(topic);
}

function initChart() {
  if (!window.echarts) {
    els.chart.innerHTML = '<div class="empty-state">Interactive chart library did not load. Check network access to the ECharts CDN.</div>';
    return null;
  }
  if (!state.chart && els.chart.querySelector('.empty-state')) {
    els.chart.innerHTML = '';
  }
  if (state.chart && !els.chart.querySelector('canvas')) {
    state.chart.dispose();
    state.chart = null;
    els.chart.innerHTML = '';
  }
  if (!state.chart) state.chart = window.echarts.init(els.chart, null, { renderer: 'canvas' });
  return state.chart;
}

function makeLineSeries(name, data, color, opts = {}) {
  return {
    name,
    type: 'line',
    data,
    showSymbol: opts.showSymbol ?? true,
    symbolSize: opts.symbolSize ?? 6,
    smooth: opts.smooth ?? true,
    connectNulls: false,
    lineStyle: { width: opts.width || 3, type: opts.dash ? 'dashed' : 'solid', color, opacity: opts.opacity ?? 1 },
    itemStyle: { color, opacity: opts.opacity ?? 1 },
    emphasis: { disabled: true },
    blur: { disabled: true },
    z: opts.z || 3
  };
}

function setChartToolsVisible(next) {
  state.chartToolsVisible = Boolean(next);
  if (!state.chart) return;
  state.chart.setOption({ toolbox: { show: state.chartToolsVisible, left: 'center', top: 8, orient: 'horizontal' } });
}

function setChartMaxed(next) {
  state.chartMaxed = Boolean(next);
  document.body.classList.toggle('chart-maxed', state.chartMaxed);
  if (els.chartMaxBtn) {
    els.chartMaxBtn.textContent = state.chartMaxed ? 'Restore' : 'Max';
    els.chartMaxBtn.setAttribute('aria-pressed', String(state.chartMaxed));
  }
  requestAnimationFrame(() => {
    if (state.chart) state.chart.resize();
    setTimeout(() => { if (state.chart) state.chart.resize(); }, 180);
  });
}

function drawInteractiveChart(topic) {
  const chart = initChart();
  if (!chart) return;
  const series = topic.series || [];
  if (!series.length) {
    if (state.chart) {
      state.chart.dispose();
      state.chart = null;
    }
    els.chart.innerHTML = '<div class="empty-state">No retention series yet. Run the update workflow to fetch live RSS feeds.</div>';
    els.chartLegend.innerHTML = '';
    els.chartNote.textContent = 'Waiting for live feed history.';
    return;
  }
  const c = chartColors();
  const tau = topic.fit?.tau_hours;
  const beta = topic.fit?.beta;
  const xs = series.map(d => Number(d.x_hours) || 0);
  const maxX = Math.max(1, ...xs);
  const primaryName = state.signal === 'corrected' ? 'observed corrected' : 'observed raw';
  const altName = state.signal === 'corrected' ? 'raw feed' : 'circadian corrected';
  const observed = series.map(d => [Number(d.x_hours) || 0, observedValue(d)]);
  const alternate = series.map(d => [Number(d.x_hours) || 0, observedValue(d, state.signal === 'corrected' ? 'raw' : 'corrected')]);
  const fit = series.map(d => [Number(d.x_hours) || 0, fitValue(d, topic)]);
  const exp = series.map(d => [Number(d.x_hours) || 0, tau ? Math.exp(-Math.max(0, Number(d.x_hours) || 0) / tau) : null]);
  const residuals = series.map(d => [Number(d.x_hours) || 0, observedValue(d) === null ? null : observedValue(d) - fitValue(d, topic)]);
  const factors = series.map(d => [Number(d.x_hours) || 0, d.circadian_factor ?? null]);
  const horizon = series.map(d => [Number(d.x_hours) || 0, d.horizon_score == null ? null : Number(d.horizon_score) / 100]);
  const deviation = series.map(d => [Number(d.x_hours) || 0, d.deviation_ratio ?? null]);
  const crossScale = series.map(d => [Number(d.x_hours) || 0, d.cross_scale_coherence ?? null]);

  const chartSeries = [];
  let yMin = 0;
  let yMax = 1.1;
  let yName = 'retention';
  if (state.view === 'horizon') {
    yName = 'horizon';
    yMax = 1.15;
    chartSeries.push(makeLineSeries('horizon score', horizon, c.bad, { width: 3.4, z: 6 }));
    chartSeries.push(makeLineSeries('cross-scale coherence', crossScale, c.good, { width: 2.2, dash: true, symbolSize: 5, z: 4 }));
    chartSeries.push(makeLineSeries('deviation / 2', deviation.map(d => [d[0], d[1] == null ? null : Math.min(1.5, d[1] / 2)]), c.accent2, { width: 2.0, dash: true, symbolSize: 5, z: 3 }));
    chartSeries.push(makeLineSeries('alert line', [[0, .70], [maxX, .70]], c.warn, { showSymbol: false, smooth: false, width: 1.5, dash: true, opacity: .8, z: 2 }));
  } else if (state.view === 'residuals') {
    yName = 'residual';
    const vals = residuals.map(d => d[1]).filter(v => v !== null);
    const maxAbs = Math.max(0.1, ...vals.map(v => Math.abs(v))) * 1.2;
    yMin = -maxAbs;
    yMax = maxAbs;
    chartSeries.push({
      name: 'residual dust',
      type: 'bar',
      data: residuals,
      barWidth: 12,
      itemStyle: { color: params => (params.value[1] || 0) >= 0 ? c.bad : c.good, borderRadius: [4, 4, 0, 0] },
      emphasis: { disabled: true },
      blur: { disabled: true },
      z: 4
    });
    chartSeries.push(makeLineSeries('zero', [[0,0],[maxX,0]], c.line, { showSymbol: false, smooth: false, width: 1, dash: true, opacity: 0.85, z: 1 }));
  } else {
    const all = [...observed, ...fit].map(d => d[1]).filter(v => v !== null);
    yMax = Math.max(1.05, ...all) * 1.05;
    chartSeries.push(makeLineSeries('S2 fit', fit, c.accent, { showSymbol: false, width: 4, z: 4 }));
    chartSeries.push(makeLineSeries(primaryName, observed, c.accent2, { width: 2.5, dash: true, symbolSize: 7, z: 5 }));
    chartSeries.push(makeLineSeries(altName, alternate, c.muted, { width: 1.5, dash: true, symbolSize: 4, opacity: 0.5, z: 2 }));
    if (state.view === 'comparison') chartSeries.push(makeLineSeries('D=1 exponential', exp, c.warn, { showSymbol: false, width: 2.4, dash: true, z: 3 }));
  }

  if (state.signal === 'corrected' && state.view !== 'residuals' && state.view !== 'horizon') {
    chartSeries.push({
      name: 'activity factor',
      type: 'line',
      yAxisIndex: 1,
      data: factors,
      showSymbol: false,
      smooth: true,
      lineStyle: { width: 1.2, color: c.good, opacity: 0.35, type: 'dotted' },
      itemStyle: { color: c.good, opacity: 0.35 },
      emphasis: { disabled: true },
      blur: { disabled: true },
      tooltip: { valueFormatter: v => v == null ? '-' : fmtNumber(v, 2) },
      z: 1
    });
  }

  const markLineData = tau ? [{ xAxis: Math.min(maxX, tau), label: { formatter: `lambda_q ${fmtHours(tau)}` } }] : [];
  if (markLineData.length && chartSeries[0]) {
    chartSeries[0].markLine = {
      symbol: ['none', 'none'],
      label: { color: c.warn, fontWeight: 800, formatter: `lambda_q ${fmtHours(tau)}` },
      lineStyle: { color: c.warn, width: 2, type: 'dashed' },
      data: markLineData
    };
  }

  const option = {
    backgroundColor: 'transparent',
    color: [c.accent, c.accent2, c.warn, c.good, c.bad],
    animationDuration: 450,
    grid: {
      left: 48,
      right: state.signal === 'corrected' && state.view !== 'residuals' ? 52 : 24,
      top: 46,
      bottom: 62,
      containLabel: true
    },
    tooltip: {
      trigger: 'axis',
      confine: false,
      appendToBody: true,
      enterable: false,
      transitionDuration: 0.05,
      axisPointer: { type: 'line', label: { color: c.text, backgroundColor: 'rgba(60,70,95,.92)' } },
      extraCssText: 'max-width:270px;white-space:normal;pointer-events:none;box-shadow:0 10px 30px rgba(0,0,0,.18);',
      backgroundColor: state.theme === 'dark' ? 'rgba(9,14,30,.96)' : 'rgba(255,255,255,.98)',
      borderColor: c.line,
      textStyle: { color: c.text },
      formatter(params) {
        const x = Array.isArray(params) ? params[0]?.axisValue : null;
        const rows = [`<b>${topic.label}</b>`, `<span>${fmtNumber(x, 1)}h since peak</span>`];
        (params || []).forEach(p => {
          const val = Array.isArray(p.value) ? p.value[1] : p.value;
          if (val === null || val === undefined || Number.isNaN(Number(val))) return;
          rows.push(`${p.marker}${p.seriesName}: <b>${fmtNumber(val, 3)}</b>`);
        });
        if (topic.isArchivedCycle) rows.push(`<span>archived peak: ${fmtDate(topic.peak_at)}</span>`);
        else rows.push(`<span>circadian bias: ${topic.circadian_bias == null ? '-' : pct(topic.circadian_bias)}</span>`);
        return rows.join('<br/>');
      }
    },
    legend: { show: false },
    toolbox: {
      show: state.chartToolsVisible,
      left: 'center',
      top: 8,
      orient: 'horizontal',
      itemSize: 14,
      itemGap: 10,
      iconStyle: { borderColor: c.muted },
      emphasis: { iconStyle: { borderColor: c.accent } },
      feature: { dataZoom: { yAxisIndex: 'none' }, restore: {}, saveAsImage: { backgroundColor: state.theme === 'dark' ? '#0a0f1e' : '#eef3fb' } }
    },
    dataZoom: [
      { type: 'inside', throttle: 40, xAxisIndex: 0 },
      {
        type: 'slider',
        xAxisIndex: 0,
        height: 12,
        bottom: 8,
        borderColor: c.line,
        fillerColor: state.theme === 'dark' ? 'rgba(125,211,252,.16)' : 'rgba(3,105,161,.16)',
        handleStyle: { color: c.accent },
        textStyle: { color: c.muted, fontSize: 10 }
      }
    ],
    xAxis: {
      type: 'value',
      min: 0,
      max: maxX,
      axisLabel: { color: c.muted, margin: 12, formatter: v => `${fmtNumber(v, 0)}h` },
      axisLine: { lineStyle: { color: c.line } },
      splitLine: { lineStyle: { color: c.line, type: 'dashed', opacity: 0.65 } }
    },
    yAxis: [
      { type: 'value', name: yName, min: yMin, max: yMax, nameTextStyle: { color: c.muted }, axisLabel: { color: c.muted }, axisLine: { lineStyle: { color: c.line } }, splitLine: { lineStyle: { color: c.line, type: 'dashed', opacity: 0.65 } } },
      { type: 'value', name: 'activity', min: 0, max: 1.15, show: state.signal === 'corrected' && state.view !== 'residuals', nameTextStyle: { color: c.muted }, axisLabel: { color: c.muted }, axisLine: { lineStyle: { color: c.line } }, splitLine: { show: false } }
    ],
    series: chartSeries
  };
  chart.setOption(option, true);

  const scopeNote = state.scope === 'cycles' ? 'Archived completed-cycle view. ' : '';
  const status = topic.fit?.fit_status === 'provisional' ? 'Provisional publish-time guide: formal S2 metrics unlock after enough post-peak published-article bins accumulate. ' : '';
  const norm = state.signal === 'corrected' ? 'Circadian-corrected semantic-topic signal divides raw publish-time counts by expected publishing/activity availability before S2 fitting.' : 'Raw mode shows the uncorrected RSS publish-time semantic-topic stream; overnight gaps may mimic decay.';
  const semantic = topic.coherence_score == null ? '' : ` semantic coherence=${fmtNumber(topic.coherence_score, 2)}.`;
  const h = topic.event_horizon || {};
  const hnote = ` horizon=${fmtNumber(h.score ?? 0, 0)} (${h.phase || 'warming up'}), H=${fmtNumber(h.index, 2)}, dH=${fmtNumber(h.slope, 3)}.`;
  els.chartNote.textContent = `${scopeNote}${status}${norm} tau=${fmtHours(tau)}, beta=${fmtNumber(beta, 2)}, half-life=${fmtHours(halfLife(tau, beta))}.${semantic}${hnote}`;
  els.chartLegend.innerHTML = chartSeries.filter(s => s.name !== 'zero').map(s => `<span><i style="background:${s.lineStyle?.color || c.bad}"></i>${s.name}</span>`).join('');
}

function renderTopicBoard() {
  const topics = getTopicsSorted();
  els.boardTitle.textContent = state.scope === 'cycles' ? 'Cycle archive' : 'Live cycles';
  if (!topics.length) {
    els.topicBoard.innerHTML = state.scope === 'cycles'
      ? '<div class="empty-state">Cycle archive is initializing. Completed formal cycles will appear here after update runs archive prior tails.</div>'
      : '<div class="empty-state">No topic cycles yet.</div>';
    return;
  }
  els.topicBoard.innerHTML = topics.map(topic => {
    const fit = topic.fit || {};
    const isFormal = fit.fit_status === 'formal';
    const realSeries = (topic.series || []).filter(d => observedValue(d) !== null);
    const retained = realSeries.length ? observedValue(realSeries[realSeries.length - 1]) : (topic.series?.length ? fitValue(topic.series[topic.series.length - 1], topic) : 0);
    const newest = topicNewest(topic);
    let badge, width, statusText, barTitle;
    if (state.scope === 'cycles') {
      const hscore = topicHorizon(topic);
      badge = state.sort === 'horizon' ? `H ${fmtNumber(hscore, 0)}` : `${fmtNumber(topic.max_stickiness || topicStickiness(topic), 0)}`;
      width = Math.min(100, Math.round(state.sort === 'horizon' ? hscore : (topic.max_stickiness || topicStickiness(topic))));
      statusText = `${topic.phase || 'archived'} · ${topicHorizonLabel(topic)} · peak ${shortDate(topic.peak_at)}`;
      barTitle = state.sort === 'horizon' ? `Archived event horizon ${hscore}/100` : `Archived stickiness ${badge}/100`;
    } else if (isFormal) {
      const hscore = topicHorizon(topic);
      badge = state.sort === 'horizon' ? `H ${fmtNumber(hscore, 0)}` : pct(retained);
      width = Math.min(100, Math.round(state.sort === 'horizon' ? hscore : ((retained || 0) * 100)));
      statusText = `${topic.phase || 'formal S2'} · ${topicHorizonLabel(topic)}`;
      barTitle = state.sort === 'horizon' ? `Event horizon score: ${hscore}/100` : `Retained signal: ${badge}`;
    } else {
      const ready = tailReadiness(topic);
      badge = `${ready.percent}%`;
      width = ready.percent;
      statusText = `${ready.label} · ${ready.needText}`;
      barTitle = `Tail readiness: ${ready.percent}%`;
    }
    return `<article class="topic-card ${topic.key === (state.scope === 'cycles' ? state.selectedCycle : state.selectedTopic) ? 'active' : ''}" data-topic="${topic.key}">
      <div class="topic-top"><span class="topic-name">${topic.label}</span><span class="topic-badge">${badge}</span></div>
      <p class="topic-meta"><span>${statusText}</span><span>N ${topic.article_count}</span><span>${state.scope === 'cycles' ? 'arch ' + shortDate(topic.archived_at) : 'new ' + (newest ? ageLabel(new Date(newest).toISOString()) : '-')}</span><span>tau ${fmtHours(fit.tau_hours)}</span><span>beta ${fmtNumber(fit.beta, 2)}</span><span>${topic.coherence_score == null ? '' : 'coh ' + fmtNumber(topic.coherence_score, 2)}</span><span>H ${fmtNumber((topic.event_horizon || {}).score ?? 0, 0)}</span><span>${state.scope === 'cycles' ? 'dust ' + fmtNumber(topicDust(topic), 3) : 'sleep ' + (topic.circadian_bias == null ? '-' : pct(topic.circadian_bias))}</span></p>
      <div class="bar" title="${barTitle}"><i style="width:${width}%"></i></div>
    </article>`;
  }).join('');
  els.topicBoard.querySelectorAll('.topic-card').forEach(card => card.addEventListener('click', () => {
    if (state.scope === 'cycles') state.selectedCycle = card.dataset.topic;
    else state.selectedTopic = card.dataset.topic;
    render();
  }));
}

function renderTopicTable() {
  els.tableTitle.textContent = state.scope === 'cycles' ? 'Archived cycle table' : 'Topic S2 table';
  els.topicTable.innerHTML = '';
  getTopicsSorted().forEach(topic => {
    const fit = topic.fit || {};
    const newest = topicNewest(topic);
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><span class="topic-pill">${topic.label}</span></td>
      <td>${topic.phase}</td>
      <td>${fmtNumber((topic.event_horizon || {}).score ?? 0, 0)}</td>
      <td>${topic.article_count}</td>
      <td>${state.scope === 'cycles' ? shortDate(topic.peak_at) : (newest ? ageLabel(new Date(newest).toISOString()) : '-')}</td>
      <td>${fmtHours(fit.tau_hours)}</td>
      <td>${fmtNumber(fit.beta, 2)}</td>
      <td>${fmtHours(halfLife(fit.tau_hours, fit.beta))}</td>
      <td>${fmtNumber(topicDust(topic), 3)}</td>
      <td>${fmtNumber(fit.delta_aic_vs_exp, 2)}</td>`;
    tr.addEventListener('click', () => {
      if (state.scope === 'cycles') state.selectedCycle = topic.key;
      else state.selectedTopic = topic.key;
      render();
    });
    els.topicTable.appendChild(tr);
  });
}

function renderStories() {
  const topic = selectedTopic();
  els.storyTitle.textContent = state.scope === 'cycles' ? 'Archived survivors' : 'Stories that stick';
  els.storyList.innerHTML = '';
  if (!topic) {
    els.storyCount.textContent = '0 shown';
    els.storyList.innerHTML = '<div class="empty-state">No stories yet for this scope.</div>';
    return;
  }

  if (state.scope === 'cycles') {
    const stories = (topic.sticky_stories || []).slice(0, 18);
    els.storyCount.textContent = `${stories.length} archived`;
    if (!stories.length) {
      els.storyList.innerHTML = '<div class="empty-state">No archived survivors for this cycle.</div>';
      return;
    }
    stories.forEach(story => appendStory(story, { archived: true }));
    return;
  }

  const articles = state.data?.articles || [];
  const selected = topic?.key;
  const formal = topic?.fit?.fit_status === 'formal';
  let filtered = selected ? articles.filter(article => article.topic === selected) : [...articles];
  if (formal) {
    filtered = filtered
      .map(article => ({ article, sticky: storyStickiness(article, topic) }))
      .sort((a, b) => {
        if (b.sticky.score !== a.sticky.score) return b.sticky.score - a.sticky.score;
        return new Date(b.article.published_at).getTime() - new Date(a.article.published_at).getTime();
      })
      .slice(0, 18);
    els.storyCount.textContent = `${filtered.length} sticky-ranked`;
  } else {
    filtered = filtered
      .sort((a, b) => new Date(b.published_at).getTime() - new Date(a.published_at).getTime())
      .slice(0, 18)
      .map(article => ({ article, sticky: storyStickiness(article, topic) }));
    els.storyCount.textContent = `${filtered.length} latest while collecting tail`;
  }
  if (!filtered.length) {
    els.storyList.innerHTML = '<div class="empty-state">No live stories yet for this topic. Run the scheduled update workflow or wait for the next RSS batch.</div>';
    return;
  }
  filtered.forEach(({ article, sticky }) => appendStory(article, { sticky, formal }));
}

function appendStory(article, opts = {}) {
  const node = els.storyTemplate.content.cloneNode(true);
  const sticky = opts.sticky || storyStickiness(article, selectedTopic());
  const meta = [article.source || 'Unknown', fmtDate(article.published_at), ageLabel(article.published_at)];
  if (opts.archived || opts.formal) {
    meta.push(`stick ${sticky.score ?? article.stickiness_score ?? 0}/100`);
    const residual = sticky.residual ?? article.residual_contribution ?? article.residual;
    if (residual != null) meta.push(`res +${fmtNumber(residual, 3)}`);
    const xh = sticky.xHours ?? article.age_after_peak_hours;
    if (xh != null) meta.push(`${fmtHours(xh)} after peak`);
    const hs = sticky.horizonScore ?? article.horizon_score;
    if (hs != null) meta.push(`H ${fmtNumber(hs, 0)}`);
    const hp = sticky.horizonPhase ?? article.horizon_phase;
    if (hp) meta.push(hp);
    meta.push(sticky.role || article.role || 'survivor');
  }
  node.querySelector('.story__meta').textContent = meta.join(' · ');
  const link = node.querySelector('a');
  link.href = article.url || '#';
  link.textContent = article.title || '(untitled)';
  if (!article.url) link.removeAttribute('href');
  node.querySelector('.story__topic').textContent = opts.archived || opts.formal
    ? `stick ${sticky.score ?? article.stickiness_score ?? 0}`
    : (article.topic_label || article.topic || 'General');
  els.storyList.appendChild(node);
}

function renderSources() {
  const sources = state.data?.sources || [];
  const errors = state.data?.summary?.fetch_errors || [];
  const errorMap = new Map(errors.map(e => [e.source, e.error]));
  els.sourceCount.textContent = `${sources.length} configured`;
  if (!sources.length) {
    els.sourceList.innerHTML = '<div class="empty-state">No sources listed. Add RSS feeds in scripts/sources.json.</div>';
    return;
  }
  els.sourceList.innerHTML = sources.map(source => {
    const err = errorMap.get(source.name);
    const label = err ? 'error' : 'ok';
    const home = source.home || source.url || '#';
    return `<article class="source-card ${err ? 'error' : ''}">
      <div><a href="${home}" target="_blank" rel="noopener noreferrer">${source.name}</a><p class="source-meta"><span>${err ? err : source.url}</span></p></div>
      <span class="source-badge">${label}</span>
    </article>`;
  }).join('');
}

els.topicSelect.addEventListener('change', event => {
  if (state.scope === 'cycles') state.selectedCycle = event.target.value;
  else state.selectedTopic = event.target.value;
  render();
});
els.viewSelect.addEventListener('change', event => { state.view = event.target.value; renderSelected(); });
els.signalSelect.addEventListener('change', event => { state.signal = event.target.value; localStorage.setItem('dream-news-signal', state.signal); render(); });
els.sortSelect.addEventListener('change', event => { state.sort = event.target.value; localStorage.setItem('dream-news-sort', state.sort); render(); });
els.scopeSelect.addEventListener('change', event => { state.scope = event.target.value; localStorage.setItem('dream-news-scope', state.scope); render(); });
els.themeBtn.addEventListener('click', () => { state.theme = state.theme === 'dark' ? 'light' : 'dark'; applyTheme(); if (state.chart) state.chart.dispose(); state.chart = null; renderSelected(); });
els.refreshBtn.addEventListener('click', () => loadData({ quiet: false }));
els.chartMaxBtn?.addEventListener('click', () => setChartMaxed(!state.chartMaxed));

if (els.chart) {
  els.chart.setAttribute('tabindex', '0');
  els.chart.addEventListener('mouseenter', () => setChartToolsVisible(true));
  els.chart.addEventListener('mouseleave', () => setChartToolsVisible(false));
  els.chart.addEventListener('focusin', () => setChartToolsVisible(true));
  els.chart.addEventListener('focusout', () => setChartToolsVisible(false));
}

document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && state.chartMaxed) setChartMaxed(false);
});
window.addEventListener('resize', () => { if (state.chart) state.chart.resize(); });

applyTheme();
loadData({ quiet: false });
setInterval(() => loadData({ quiet: true }), REFRESH_MS);
