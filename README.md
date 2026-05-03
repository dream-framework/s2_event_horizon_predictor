# S2 Event Horizon — DREAM Live News Retention Monitor

Compact GitHub Pages app for live RSS news-cycle monitoring with stable semantic topic tracking, DREAM S2 retention fits, cycle archives, and publish-time reconstruction:

```text
R(lambda) = exp[-(lambda / lambda_q)^D_eff]
```

The app is static. GitHub Actions fetches RSS, updates `data/history.json` and `data/news_s2.json`, fits the topic decay curves, and redeploys the static frontend. No Render.com, server, database, or always-on backend is needed for this MVP.

## What changed in this build

- Live-feed only: simulation controls are removed.
- Interactive ECharts charting: hover tooltips, zoom/pan, restore, and save-as-image.
- Semantic topic tracking: raw RSS articles are converted into stable topic objects before S2 fitting.
- Publish-time reconstruction: curves are built from real article `published_at` timestamps, not merely from GitHub Action snapshot times.
- Circadian correction: formal S2 fits use publish-time article counts corrected for expected global sleeping/publishing cycles.
- Signal mode selector: compare `Circadian-corrected` versus `Raw feed` directly.
- The chart overlays the alternate signal in a faint line so overnight feed gaps are visible.
- Theme toggle label is explicit: `Theme: Dark` / `Theme: Light`.
- Compact landscape layout with a smaller top bar.
- Source-health panel shows configured RSS feeds and fetch errors from the latest workflow run.
- Expanded default feed list, including more world, technology, science, market, health, geopolitics, climate, and Google News RSS feeds.
- Soft-refresh behavior: the static shell stays loaded while the browser polls `data/news_s2.json` every 5 minutes and updates values/charts in place.
- Hover/focus behavior fixed so ECharts no longer fades the whole graph on focus.
- Initial bundled JSON is intentionally empty/pending so the deployed site does not show fake `example.com` stories. Run the update workflow once to populate real feeds.


## Publish-time reconstruction

The natural observation clock is the article publication time, not the GitHub Action run time. This build keeps both:

```text
Reality layer:      article title, source, URL, published_at
Measurement layer:  first_seen_at / last_seen_at from GitHub fetches
Model layer:        topic bins built from published_at, then circadian-corrected and fit to S2
```

This avoids waiting for GitHub to watch a story from scratch when the RSS feed already contains articles from the last several hours or days. It does **not** invent missing data. It only reconstructs the topic curve from actual timestamps already present in the feeds.

Formal S2 metrics still stay locked until enough post-peak publish-time bins exist. Until then, the chart is labeled as a provisional guide and the topic board shows `LIVE` rather than a claimed retained percentage.

## Circadian correction

Raw news volume is not pure information retention. People sleep, editors publish on schedules, and RSS sources have regional rhythms. This version treats the observed stream as:

```text
observed published-time articles = S2 retention x activity availability x source cadence x aftershocks
```

Before formal S2 fitting, each article is placed into a publish-time bin and weighted by an approximate global activity factor based on UTC time, using a mix of Americas, Europe/Africa, and Asia-Pacific daily cycles. The generated JSON stores both:

```text
observed_raw
observed_corrected
circadian_factor
circadian_bias
```

Use the dashboard `Signal` selector to switch between raw and corrected views. The corrected signal is the default for S2 metrics; raw mode is useful for diagnosing overnight gaps and publishing artifacts.

## Interactive chart behavior

The chart uses ECharts from a CDN:

```html
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
```

You get:

- hover/crosshair tooltips,
- mouse-wheel or trackpad zoom,
- drag-to-pan,
- restore button,
- save-as-image button,
- S2 fit, observed signal, raw/corrected comparison, D=1 exponential baseline, and residual bars.

If the chart does not load, check whether the browser/network can access the ECharts CDN.

## Repository structure

```text
.
├── index.html
├── assets/
│   ├── app.js
│   └── styles.css
├── data/
│   ├── history.json      # live retention history; do not overwrite after deployment
│   ├── news_s2.json      # current active-wave report
│   └── cycles.json       # archived completed cycle summaries
├── scripts/
│   ├── sources.json
│   └── update_news.py
└── .github/workflows/
    ├── deploy-pages.yml
    └── update-news.yml
```

## Local preview

From the repo root:

```bash
python3 -m http.server 8000
```

Open:

```text
http://localhost:8000
```

The bundled JSON starts empty. To fetch live RSS locally:

```bash
python3 scripts/update_news.py
python3 -m http.server 8000
```

To rebuild only from existing `data/history.json` without fetching:

```bash
python3 scripts/update_news.py --no-fetch
```

Optional local demo data still exists for testing the chart shape, but it is not used by default:

```bash
python3 scripts/update_news.py --sample
```

## Deploy on GitHub Pages

### 1. Create or update the repo

Use one repo per app. Recommended repo/page name for this project:

```text
s2_event_horizon
```

Push these files to `main`.

### 2. Enable Pages

In GitHub:

```text
Repo -> Settings -> Pages -> Build and deployment -> Source -> GitHub Actions
```

### 3. Deploy the shell

Run:

```text
Actions -> Deploy static app -> Run workflow
```

This publishes the frontend with pending/empty live-feed JSON.

### 4. Fetch real feeds and redeploy

Run:

```text
Actions -> Update news S2 data and deploy -> Run workflow
```

That workflow:

1. fetches RSS feeds from `scripts/sources.json`,
2. converts articles into sparse TF-IDF/entity semantic vectors,
3. clusters articles into stable semantic topic objects,
4. updates `data/history.json` and `data/topic_memory.json`,
5. reconstructs publish-time topic histograms from real article timestamps,
6. applies circadian correction,
7. fits S2 retention curves on the corrected signal,
8. writes `data/news_s2.json` and `data/cycles.json`,
9. commits the JSON,
10. redeploys GitHub Pages.

After it completes, refresh your GitHub Pages URL.

## Why topics can still show as collecting evidence

A formal S2 fit needs a visible post-peak cooling tail: several nonzero publish-time bins after the topic peak. Even with many articles, a topic can still be actively peaking. The app labels these as `Collecting post-peak evidence`, `Sparse publish-time signal`, or `Provisional S2 guide` instead of forcing a fake fit. Formal `log-R2` and `Delta AIC` appear once enough post-peak published-article bins exist.

## Scheduled updates

The live update workflow runs hourly by default:

```yaml
schedule:
  - cron: '17 * * * *'
```

GitHub cron is UTC. Change that line in `.github/workflows/update-news.yml` to adjust cadence.

A more scheduler-friendly production cadence is every two hours:

```yaml
schedule:
  - cron: '23 */2 * * *'
```

## Add or remove RSS feeds

Edit:

```text
scripts/sources.json
```

Each source looks like:

```json
{
  "name": "BBC World",
  "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
  "home": "https://www.bbc.com/news/world"
}
```

The UI source-health panel reads this list from the generated `data/news_s2.json`.

## Semantic topic layer

This build no longer treats broad keyword buckets as the main S2 object. It uses a lightweight, dependency-free semantic tracker in `scripts/update_news.py`:

```text
article title + summary
-> sparse TF-IDF vector + simple entity set + broad sector prior
-> semantic connected components
-> persistent topic identity via centroid inertia
-> coherence filtering
-> publish-time S2 signal
```

The persistent identity file is:

```text
data/topic_memory.json
```

It stores stable topic keys, smoothed centroids, keywords, entities, drift, coherence, and last-seen timestamps. This is the macroscopic object that survives raw article/vector drift. When updating a live repo, preserve this file if it exists.

You can tune the broad sector priors by editing the `TOPICS` dictionary in `scripts/update_news.py`. These are no longer the final topic identities; they are guardrails that help prevent unrelated sectors from merging.

## Updating an existing live repo safely

When applying this package to an existing deployed repo, preserve your accumulated live history:

```text
Keep:     data/history.json, data/topic_memory.json, data/cycles.json if present
Replace:  index.html, assets/, scripts/, .github/workflows/, data/news_s2.json if desired
```

`history.json` is the retained evidence trail. Overwriting it resets the app back to early warmup.

## How to read the dashboard

- `lambda_q / tau`: coherence window of the topic cycle, in elapsed hours since the reconstructed publish-time peak.
- `D_eff / beta`: decay exponent. Higher values mean sharper cliffs; values below 1 indicate longer tails.
- `Half-life`: when retained attention reaches 50%.
- `log-R2`: fit quality in log-retention space.
- `Delta AIC`: positive values mean S2 beats a plain D=1 exponential.
- `Sleep bias`: how strongly circadian correction changed the topic signal.
- `Dust`: late residual after subtracting the S2 fit.
- `Source health`: which RSS feeds were configured and whether any failed in the last run.

## When to move off GitHub Pages

Stay on GitHub Pages until you need sub-minute ingestion, logins, user-specific queries, a real database, large-scale embeddings/clustering, or local LLM summarization. For live RSS + hourly JSON + static visualization, GitHub Pages is enough.

## Cycle archive mode

This rebuild adds a top-level scope toggle:

```text
Current wave | Cycle archive
```

`Current wave` is the live measurement surface. It reports the newest active wave per topic, with tail readiness while the post-peak evidence is still accumulating.

`Cycle archive` is retained learning. It reads `data/cycles.json`, which stores completed formal cycle summaries from earlier runs. A new burst can reset the current wave without erasing the prior S2 result; the prior result becomes an archived cycle with its own lambda_q, beta, half-life, Delta AIC, dust, and sticky stories.

The data layers are now deliberately separate:

```text
data/history.json   raw immutable article memory and first_seen/last_seen provenance
data/news_s2.json   current active-wave report
data/cycles.json    archived completed cycle summaries
data/topic_memory.json stable semantic topic identities and centroid inertia
```

The workflow now commits all three JSON files when they change:

```text
data/history.json
data/news_s2.json
data/cycles.json
data/topic_memory.json
```

When updating an already-live repo, preserve both accumulated memory files if they already exist:

```text
Keep:     data/history.json, data/topic_memory.json, data/cycles.json
Replace:  index.html, assets/, scripts/, .github/workflows/, data/news_s2.json if desired
```

## Stories that stick

The story panel now changes behavior by state:

```text
Current wave, warmup/provisional: latest stories while collecting tail
Current wave, formal S2: sticky-ranked residual leaders
Cycle archive: archived post-lambda_q survivors
```

The score is a compact 0-100 stickiness indicator based on positive residual above the S2 baseline, age after peak, and whether the story survived beyond lambda_q. It is not the same as the topic retained percentage.

## Chart controls

The ECharts toolbox is hidden until the chart is hovered or focused, then appears centered at the top so it does not obscure the second y-axis. The `Max` button expands the chart to fill the browser viewport; `Restore` or `Esc` returns to the dashboard.

## S2 event-horizon detector

This build adds an explicit event-horizon layer. The idea is not to detect what is merely big or trending. It detects topics whose observed persistence exceeds the S2 decay envelope while also becoming coherent across source scales.

The generated JSON stores, per topic and per time point:

```text
Deviation(t) = observed_corrected(t) / expected_S2(t)
CrossScaleCoherence(t) = source-tier spread + source diversity + semantic coherence
H(t) = Deviation(t) * CrossScaleCoherence(t)
```

Phases:

```text
Noise: low deviation, low cross-scale coherence
Local anomaly: high deviation but low cross-scale coherence
Pre-horizon: high H and rising H; early warning state
Post-horizon: high H and high coherence; already broadly realized
Normal S2 decay: follows the expected decay envelope
```

The dashboard includes:

- `Event horizon` sort mode,
- `Event horizon` chart view,
- a Horizon metric pill,
- per-topic `H` score and phase,
- per-story horizon score/phase when available.

This layer is intentionally separate from the S2 fit. S2 remains the baseline law; the horizon detector asks what can no longer collapse back under that baseline.
