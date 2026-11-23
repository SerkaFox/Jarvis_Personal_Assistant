# GitHub feed of verified IT vacancies

This directory is a **data-only exchange** for the user's separate job application/autofill app. No Telegram integration, bot runtime, server deployment, job submission, or automated applications are part of this integration.

## Current feed

**URL:** https://raw.githubusercontent.com/SerkaFox/Jarvis_Personal_Assistant/main/jobs/feed.json

The latest batch is a JSON object with `schema_version: 1`, `generated_at` (ISO-8601 timestamp), and `jobs` (0–8 records). An empty `jobs` array means no verified fresh vacancies were found in that batch. **No fabricated examples are inserted into the live feed.**

Example of the record format (documentation only, NOT a real listing):

```json
{
  "company": "Example Company",
  "title": "Python Backend Developer",
  "url": "https://example.com/jobs/123",
  "location": "Bilbao, Bizkaia",
  "work_mode": "remote",
  "published_at": "2026-11-23",
  "match_score": 88,
  "why_match": "Django, REST API, PostgreSQL and Linux",
  "gaps": "English C1 required",
  "application_es": "Soy desarrollador con más de siete años de experiencia...",
  "application_ru": "Я разработчик с опытом более семи лет..."
}
```

All fields other than `company`, `title`, and `url` are optional. `match_score` must be an integer from 0 to 100. `published_at` is omitted when the original date cannot be confirmed. `application_es` is merely a draft; it is **never submitted**.

## Rules for curating

- Search original publications from the previous 48 hours, targeting Bilbao, Bizkaia, País Vasco, and fully remote work possible from Spain.
- Python (Django, DRF, Flask, FastAPI), Java/Spring Boot, Full Stack, DevOps/Linux, automation/integrations.
- Prioritise genuine engineering jobs; exclude sales, unpaid internships, location-incompatible hybrid, and known repeats.
- Match Sergei Svitkin's skills and language level faithfully. Include technical gaps and a direct vacancy URL.
- Never confuse the date of a refreshed advertisement with its first publication.
- Publish up to 8 verified positions. Use an empty feed when there are no confirmed new matches.
- For stronger historical coverage, store daily snapshots under `jobs/archive/YYYY-MM-DD.json` only when real vacancies are identified. Consumer apps can deduplicate by canonical URL and company/title.

## For the external consumer

Fetch the raw URL using ordinary HTTPS GET and parse JSON. Poll `generated_at` to detect new batches. Store canonical URLs (or a hashed ID derived from them) in the consumer's local database to avoid notifying repeatedly. A public GitHub repository requires no credentials for reading the raw feed. No inbound port or connection to the Jarvis bot is required.

**Important:** The feed is not updated by GitHub automatically. The scheduled ChatGPT job-search automation must be able to access the GitHub connector and commit new verified data; until that is separately confirmed, the feed is only a prepared destination.
