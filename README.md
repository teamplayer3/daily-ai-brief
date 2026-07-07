# Daily AI Brief

Static site generator for an AI-heavy daily news front page.

## Sources

- OpenAI News
- Google AI
- TechCrunch AI
- Ars Technica AI

## Run

```bash
python3 -m lib
```

This writes the generated site to `site/`.

## Options

```bash
python3 -m lib --output-dir site --days 7 --per-source 8 --limit 24
```

## Local preview

```bash
python3 -m http.server 4173 --directory site
```

## Daily refresh on GitHub Pages

The workflow in `.github/workflows/daily-ai-brief.yml` rebuilds and deploys the site every day and also supports manual runs.
