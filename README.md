# NSM - Norwegian Singles Method Running Tracker

A minimal, self-maintaining local web application that replaces an Excel spreadsheet for tracking the Norwegian Singles running program.

The app syncs automatically with [Intervals.icu](https://intervals.icu), classifies workouts, and displays a weekly training grid.

The **Stats** page tracks average work-only SubT pace and total work distance by week or month.

![screenshot](img/sample.png)

Built with Claude Opus 4.6.

## Quick Start

### Docker (recommended)

```bash
cp config.example.json config.json   # Edit with your API key
docker compose up -d
```

### Without Docker

```bash
pip install -r requirements.txt
cp config.example.json config.json   # Edit with your API key
python app.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

After updating the application code, rebuild the Docker image with
`docker compose up -d --build` so new modules and templates are included.

On the first run, the app performs an initial sync. This can take a few minutes because it fetches interval data for each activity.

## Configuration

Edit `config.json`:

| Field | Description | Default |
| --- | --- | --- |
| `api_key` | Your Intervals.icu API key, found under Settings → Developer | Required |
| `athlete_id` | Athlete ID — `0` resolves to the API key owner | `"0"` |
| `activity_types` | Activity types to include | `["Run", "TrailRun", "VirtualRun"]` |
| `weeks_initial_sync` | Number of weeks of history to fetch on the first run | `12` (3 months) |
| `sync_stale_minutes` | Auto-refresh when the page is opened and the last sync is older than this | `60` |
| `port` | Local server port | `5000` |
| `db_path` | SQLite database file path | `"data/nsm.db"` |
| `debug` | Enable verbose logging for activity counts, filters, and sync details | `false` |

## How It Works

### Units & Locale

Distance, pace, timezone, and date/time display are automatically fetched from your Intervals.icu profile on every sync.

### Architecture

```text
Intervals.icu API
        │
        ▼
  sync.py (Python, requests)
        │
        ▼
  SQLite (nsm.db)
        │
        ▼
  app.py (Flask, 2 routes)
        │
        ▼
  index.html (Bootstrap 5 CDN, Jinja2, minimal JavaScript)
```

**Dependencies:** `flask` and `requests`.

### Sync Algorithm

- **Initial sync:** Downloads all activities from the configured history period. For each run, it fetches interval data from the API to determine the workout classification.
- **Incremental refresh:** Fetches activities from `last_sync - 2 days` through the current time. The overlap helps catch edits to recent activities.
- **Auto-refresh on visit:** When you open the dashboard, it renders instantly from the local database, then refreshes in the background only if the last sync is older than `sync_stale_minutes`. A tab left open re-checks on the same interval, so it stays current without a manual reload. You can always force a refresh with the **Sync** button. 
- **Upserts:** Existing activities are updated by activity ID, while new activities are inserted.
- **Deleted activities:** The sync detects and removes activities that no longer exist in the configured date range.
- **Rate limiting:** Waits 100 ms between interval-data requests.
- **Work-metrics backfill:** On upgrade, the database schema is migrated automatically. Existing local interval sessions are backfilled once, including sessions older than the normal sync window. Starting the app launches this in the background; visiting the dashboard or clicking **Sync** also triggers it. Progress is committed every 25 candidates, completed sessions are skipped, and failures are retried on later syncs. This does not download history that was never stored locally.
- **Interval fetch failures:** Previously stored classification and work totals are preserved and marked failed rather than replaced with easy/zero-work data. New unclassified activities show as Unavailable until a successful retry.

### Workout Classification

Each activity is automatically classified using its Intervals.icu interval data:

| Classification | Rule | Display name |
| --- | --- | --- |
| **Race** | Activity `sub_type` is `RACE` — takes priority over all rules below | `Race` |
| **Interval** | Has real recoveries (at least 15 seconds) and an interval group with at least two repetitions | `SubT-{count}x{avg_minutes}` |
| **Long** | Does not qualify as intervals and moving duration is at least 60 minutes | `Long-{minutes}` |
| **Easy** | All other activities | `Easy-{minutes}` |

### Training Distribution

Weekly totals show an E/T split:

- **T (Threshold):** Actual moving seconds in `WORK` repetitions belonging to repeated interval groups.
- **E (Easy):** Total moving time minus T time. Warm-up, cool-down, and recovery intervals all count as Easy.

### SubT Stats

Open **Stats** in the header, or visit `/stats`.

- Defaults to the last 12 calendar weeks, including the current week; choose 26, 52, or all locally stored history.
- Switch between weekly and monthly aggregation. Range selections define a date window; truncated calendar months are labelled **partial range**.
- The pace line chart and distance bar chart share a timeline and synchronized horizontal scrolling. Faster pace is plotted higher.
- Expand a table period to see contributing sessions and their work-only summaries.
- Work time is the sum of actual interval moving seconds; distance is the sum of actual interval metres. Warm-up, recovery, cool-down, races, easy runs, and long runs are excluded. All qualifying interval formats are combined.
- **Average pace = total work seconds / total work distance in km (or miles).** Session paces and weekly paces are never averaged to calculate period pace.
- Ongoing periods are labelled and faded in charts. Periods without SubT show zero work and no pace. Periods with pending/failed data show known subtotals with an asterisk and are omitted from charts, rather than shown as valid zero-work periods.
- Charts are rendered locally as SVG without chart-library dependencies. Stats render from SQLite; only sync/backfill accesses the activity API.

### Tests

```bash
python -m unittest discover -s tests -v
```

Tests use temporary databases, fake configuration, and mocked API responses; they do not modify the live database or call Intervals.icu.

## Project Structure

```text
NSM/
├── app.py              # Flask server and main entry point
├── sync.py             # Intervals.icu synchronization logic
├── classify.py         # Workout classification algorithm
├── database.py         # Idempotent schema migration
├── stats.py            # Work-only aggregation and SVG chart geometry
├── config.json         # Local settings (gitignored)
├── config.example.json # Configuration template
├── schema.sql          # Database schema
├── requirements.txt    # Python dependencies
├── Dockerfile
├── docker-compose.yml
├── .gitignore
├── .dockerignore
├── templates/
│   ├── base.html       # Shared navigation, theme, and sync controls
│   ├── index.html      # Training grid
│   ├── stats.html      # Progress charts and period/session table
│   └── chart.html      # Local SVG chart template
├── static/
│   └── style.css       # Custom styles
└── data/
    └── nsm.db          # SQLite database (auto-created, gitignored)
```

The stats implementation and its tests are marked `[AI-GENERATED]` in source.

## Contributing

This is a personal tool shared publicly. Contributions are welcome, with realistic expectations:

- **Bugs** - Addressed based on severity and personal availability
- **Pull requests** - Please open an issue first to discuss it before submitting a pull request.
- **Feature requests** - May not align with my needs. Fork and customize as needed.