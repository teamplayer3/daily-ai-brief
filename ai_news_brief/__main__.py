from __future__ import annotations

import argparse
import html
import json
import re
import textwrap
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable


USER_AGENT = "ai-news-brief/0.1 (+https://localhost)"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "media": "http://search.yahoo.com/mrss/",
}


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str
    group: str
    kind: str
    weight: int


@dataclass
class Article:
    source: str
    group: str
    title: str
    link: str
    published_at: datetime
    summary: str
    author: str | None
    categories: list[str]
    image_url: str | None
    score: int

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["published_at"] = self.published_at.isoformat()
        return payload


FEEDS = [
    FeedSource(
        name="OpenAI News",
        url="https://openai.com/news/rss.xml",
        group="Frontier Labs",
        kind="lab",
        weight=120,
    ),
    FeedSource(
        name="Google AI",
        url="https://blog.google/innovation-and-ai/technology/ai/rss/",
        group="Frontier Labs",
        kind="lab",
        weight=110,
    ),
    FeedSource(
        name="TechCrunch AI",
        url="https://techcrunch.com/category/artificial-intelligence/feed/",
        group="Industry Coverage",
        kind="coverage",
        weight=80,
    ),
    FeedSource(
        name="Ars Technica AI",
        url="https://arstechnica.com/ai/feed/",
        group="Industry Coverage",
        kind="coverage",
        weight=75,
    ),
]


def fetch_xml(url: str) -> ET.Element:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=25) as response:
        return ET.fromstring(response.read())


def text_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None

    parsers = (
        lambda raw: parsedate_to_datetime(raw),
        lambda raw: datetime.fromisoformat(raw.replace("Z", "+00:00")),
    )
    for parser in parsers:
        try:
            parsed = parser(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except (TypeError, ValueError):
            continue
    return None


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    without_tags = re.sub(r"<[^>]+>", " ", value)
    without_entities = html.unescape(without_tags)
    return re.sub(r"\s+", " ", without_entities).strip()


def extract_image(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = re.search(r'src=["\']([^"\']+)["\']', value)
        if match:
            return html.unescape(match.group(1))
    return None


def first_text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path, NS)
    return text_or_none(found.text if found is not None else None)


def parse_rss(source: FeedSource, root: ET.Element, cutoff: datetime, per_source: int) -> list[Article]:
    channel = root.find("channel")
    if channel is None:
        return []

    articles: list[Article] = []
    for item in channel.findall("item"):
        title = first_text(item, "title")
        link = first_text(item, "link")
        published = parse_timestamp(first_text(item, "pubDate"))
        if not title or not link or not published or published < cutoff:
            continue

        description = first_text(item, "description")
        content_html = first_text(item, "content:encoded")
        summary = strip_html(content_html or description)
        author = first_text(item, "dc:creator")
        categories = [
            category.text.strip()
            for category in item.findall("category")
            if category.text and category.text.strip()
        ]

        media = item.find("media:content", NS)
        media_url = media.get("url") if media is not None else None
        image_url = media_url or extract_image(content_html, description)

        articles.append(
            Article(
                source=source.name,
                group=source.group,
                title=title,
                link=link,
                published_at=published,
                summary=summary,
                author=author,
                categories=dedupe(categories)[:4],
                image_url=image_url,
                score=source.weight,
            )
        )

    articles.sort(key=lambda article: article.published_at, reverse=True)
    return articles[:per_source]


def parse_atom(source: FeedSource, root: ET.Element, cutoff: datetime, per_source: int) -> list[Article]:
    articles: list[Article] = []
    for entry in root.findall("atom:entry", NS):
        title = text_or_none(first_text(entry, "atom:title"))
        link = None
        for link_node in entry.findall("atom:link", NS):
            if link_node.get("rel") in {None, "alternate"}:
                link = text_or_none(link_node.get("href"))
                if link:
                    break

        published = parse_timestamp(
            first_text(entry, "atom:published") or first_text(entry, "atom:updated")
        )
        if not title or not link or not published or published < cutoff:
            continue

        summary_html = first_text(entry, "atom:summary")
        content_html = first_text(entry, "atom:content")
        summary = strip_html(content_html or summary_html)
        author = first_text(entry, "atom:author/atom:name")
        categories = [
            category.get("term", "").strip()
            for category in entry.findall("atom:category", NS)
            if category.get("term", "").strip()
        ]
        image_url = extract_image(content_html, summary_html)

        articles.append(
            Article(
                source=source.name,
                group=source.group,
                title=strip_html(title),
                link=link,
                published_at=published,
                summary=summary,
                author=author,
                categories=dedupe(categories)[:4],
                image_url=image_url,
                score=source.weight,
            )
        )

    articles.sort(key=lambda article: article.published_at, reverse=True)
    return articles[:per_source]


def fetch_articles(days: int, per_source: int, limit: int) -> list[Article]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    articles: list[Article] = []
    failures: list[str] = []

    for source in FEEDS:
        try:
            root = fetch_xml(source.url)
            tag = root.tag.rsplit("}", 1)[-1]
            if tag == "rss":
                parsed = parse_rss(source, root, cutoff, per_source)
            elif tag == "feed":
                parsed = parse_atom(source, root, cutoff, per_source)
            else:
                parsed = []
            articles.extend(parsed)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{source.name}: {exc}")

    if not articles:
        joined = "; ".join(failures) if failures else "no articles matched the filters"
        raise RuntimeError(f"Unable to build feed: {joined}")

    deduped = dedupe_articles(articles)
    deduped.sort(
        key=lambda article: (article.published_at, article.score, article.source),
        reverse=True,
    )
    return deduped[:limit]


def dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(value)
    return ordered


def dedupe_articles(articles: Iterable[Article]) -> list[Article]:
    seen: set[str] = set()
    ordered: list[Article] = []
    for article in articles:
        key = article.link.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        ordered.append(article)
    return ordered


def truncate_summary(value: str, width: int = 220) -> str:
    if not value:
        return ""
    return textwrap.shorten(value, width=width, placeholder="...")


def format_timestamp(value: datetime) -> str:
    return value.astimezone().strftime("%b %d, %Y %H:%M")


def group_articles(articles: list[Article]) -> dict[str, list[Article]]:
    groups: dict[str, list[Article]] = {}
    for article in articles:
        groups.setdefault(article.group, []).append(article)
    return groups


def render_article_card(article: Article, featured: bool = False) -> str:
    tags = "".join(
        f'<li class="article-tag">{html.escape(tag)}</li>' for tag in article.categories[:4]
    )
    image = (
        f'<div class="article-media"><img src="{html.escape(article.image_url)}" '
        f'alt="{html.escape(article.title)}" loading="lazy"></div>'
        if article.image_url
        else ""
    )
    meta = [article.source, format_timestamp(article.published_at)]
    if article.author:
        meta.append(article.author)
    meta_html = " · ".join(html.escape(part) for part in meta if part)
    summary = truncate_summary(article.summary, 320 if featured else 200)
    classes = "article-card article-card--featured" if featured else "article-card"
    summary_html = f'<p class="article-summary">{html.escape(summary)}</p>' if summary else ""
    tags_html = f'<ul class="article-tags">{tags}</ul>' if tags else ""
    speech_text = " ".join(part for part in [article.title, summary] if part).strip()
    speak_button = (
        f'<button class="speak-card-button" type="button" '
        f'data-speech-title="{html.escape(article.title)}" '
        f'data-speech-meta="{html.escape(meta_html)}" '
        f'data-speech-summary="{html.escape(summary)}">Listen</button>'
    )

    return f"""
    <article class="{classes}" data-group="{html.escape(article.group)}">
      {image}
      <div class="article-body">
        <div class="article-meta">{meta_html}</div>
        <h3 class="article-title">
          <a href="{html.escape(article.link)}" target="_blank" rel="noreferrer">{html.escape(article.title)}</a>
        </h3>
        {summary_html}
        {tags_html}
        <div class="article-actions">
          {speak_button}
        </div>
      </div>
    </article>
    """


def render_source_table(articles: list[Article]) -> str:
    counts: dict[str, int] = {}
    groups: dict[str, str] = {}
    for article in articles:
        counts[article.source] = counts.get(article.source, 0) + 1
        groups[article.source] = article.group

    rows = []
    for source in sorted(counts):
        rows.append(
            f"""
            <div class="source-row">
              <div>
                <div class="source-name">{html.escape(source)}</div>
                <div class="source-group">{html.escape(groups[source])}</div>
              </div>
              <div class="source-count">{counts[source]}</div>
            </div>
            """
        )
    return "".join(rows)


def render_index(articles: list[Article], output_dir: Path) -> None:
    generated_at = datetime.now(UTC)
    featured = pick_featured(articles)
    remainder = [article for article in articles if article.link != featured.link]
    grouped = group_articles(remainder)
    latest = format_timestamp(articles[0].published_at)
    sources = len({article.source for article in articles})
    lab_count = len([article for article in articles if article.group == "Frontier Labs"])
    coverage_count = len([article for article in articles if article.group == "Industry Coverage"])

    section_html = []
    for label in ("Frontier Labs", "Industry Coverage"):
        section_cards = "".join(render_article_card(article) for article in grouped.get(label, []))
        section_html.append(
            f"""
            <section class="content-band">
              <div class="band-head">
                <h2>{html.escape(label)}</h2>
              </div>
              <div class="article-grid">
                {section_cards}
              </div>
            </section>
            """
        )

    payload = {
        "generated_at": generated_at.isoformat(),
        "articles": [article.to_dict() for article in articles],
    }
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    html_path = output_dir / "index.html"
    html_path.write_text(
        f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Daily AI Brief</title>
    <link rel="stylesheet" href="styles.css">
  </head>
  <body>
    <header class="hero">
      <div class="shell hero-grid">
        <div class="hero-copy">
          <p class="eyebrow">Daily AI Brief</p>
          <h1>AI-heavy signal, refreshed from labs and industry coverage.</h1>
          <p class="hero-text">A daily-generated front page built from OpenAI, Google AI, TechCrunch AI, and Ars Technica AI feeds.</p>
          <div class="filter-bar" role="tablist" aria-label="Article filters">
            <button class="filter-button is-active" data-filter="all" type="button">All</button>
            <button class="filter-button" data-filter="Frontier Labs" type="button">Labs</button>
            <button class="filter-button" data-filter="Industry Coverage" type="button">Coverage</button>
          </div>
          <div class="listen-bar" aria-label="Audio controls">
            <button class="listen-button" data-listen-target="visible" type="button">Read visible stories</button>
            <button class="listen-button" data-listen-target="lead" type="button">Read lead story</button>
            <button class="listen-button listen-button--secondary" data-listen-target="stop" type="button">Stop</button>
          </div>
          <p class="listen-status" aria-live="polite">Audio ready.</p>
        </div>
        <div class="hero-stats">
          <div class="stat">
            <span class="stat-label">Latest</span>
            <span class="stat-value">{html.escape(latest)}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Stories</span>
            <span class="stat-value">{len(articles)}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Sources</span>
            <span class="stat-value">{sources}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Lab updates</span>
            <span class="stat-value">{lab_count}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Coverage</span>
            <span class="stat-value">{coverage_count}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Generated</span>
            <span class="stat-value">{html.escape(format_timestamp(generated_at))}</span>
          </div>
        </div>
      </div>
    </header>

    <main>
      <section class="content-band">
        <div class="shell lead-grid">
          <div class="lead-story">
            <div class="band-head">
              <h2>Lead story</h2>
            </div>
            {render_article_card(featured, featured=True)}
          </div>
          <aside class="source-panel">
            <div class="band-head">
              <h2>Source mix</h2>
            </div>
            <div class="source-list">
              {render_source_table(articles)}
            </div>
          </aside>
        </div>
      </section>

      <div class="shell">
        {''.join(section_html)}
      </div>

    </main>

    <script src="site.js"></script>
  </body>
</html>
""",
        encoding="utf-8",
    )

    (output_dir / "styles.css").write_text(STYLES, encoding="utf-8")
    (output_dir / "site.js").write_text(SCRIPT, encoding="utf-8")


def build_site(output_dir: Path, days: int, per_source: int, limit: int) -> None:
    articles = fetch_articles(days=days, per_source=per_source, limit=limit)
    output_dir.mkdir(parents=True, exist_ok=True)
    render_index(articles, output_dir)


def pick_featured(articles: list[Article]) -> Article:
    frontier_articles = [article for article in articles if article.group == "Frontier Labs"]
    if frontier_articles:
        frontier_articles.sort(key=lambda article: (article.score, article.published_at), reverse=True)
        return frontier_articles[0]
    return articles[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a static AI-heavy news website.")
    parser.add_argument("--output-dir", default="site", help="Directory for generated site files.")
    parser.add_argument("--days", type=int, default=7, help="How many days of news to include.")
    parser.add_argument("--per-source", type=int, default=8, help="Max stories per source.")
    parser.add_argument("--limit", type=int, default=24, help="Total stories to keep.")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    build_site(output_dir=output_dir, days=args.days, per_source=args.per_source, limit=args.limit)
    print(f"Generated Daily AI Brief in {output_dir}")


STYLES = """
:root {
  color-scheme: dark;
  --bg: #121212;
  --panel: #1c1c1f;
  --panel-2: #232327;
  --muted: #a6abb7;
  --text: #f3f4f6;
  --line: #31333a;
  --warm: #f6b14a;
  --cool: #57c7ff;
  --green: #67d391;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: linear-gradient(180deg, #141416 0%, #101113 100%);
  color: var(--text);
}

a {
  color: inherit;
  text-decoration: none;
}

img {
  display: block;
  width: 100%;
}

.shell {
  width: min(1200px, calc(100vw - 32px));
  margin: 0 auto;
}

.hero,
.content-band {
  padding: 28px 0;
}

.content-band--muted {
  background: rgba(255, 255, 255, 0.02);
  border-top: 1px solid var(--line);
}

.hero-grid,
.lead-grid {
  display: grid;
  gap: 24px;
}

.hero-grid {
  grid-template-columns: minmax(0, 1.5fr) minmax(280px, 0.9fr);
  align-items: start;
}

.lead-grid {
  grid-template-columns: minmax(0, 1.4fr) minmax(280px, 0.8fr);
}

.eyebrow,
.stat-label,
.article-meta,
.source-group {
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-size: 0.72rem;
}

.hero h1,
.band-head h2,
.article-title {
  margin: 0;
  letter-spacing: 0;
}

.hero h1 {
  max-width: 12ch;
  font-size: clamp(2.4rem, 4vw, 4.4rem);
  line-height: 0.98;
}

.hero-text {
  max-width: 58ch;
  color: #cfd4dd;
  font-size: 1.05rem;
  line-height: 1.6;
  margin: 20px 0 0;
}

.hero-copy,
.hero-stats,
.source-panel {
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.03), rgba(255, 255, 255, 0.015));
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 24px;
}

.hero-stats {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.stat {
  min-height: 72px;
  padding: 14px;
  border: 1px solid rgba(255, 255, 255, 0.05);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.02);
}

.stat-value {
  display: block;
  margin-top: 6px;
  font-size: 1rem;
  line-height: 1.4;
}

.filter-bar {
  display: inline-flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 24px;
}

.listen-bar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 14px;
}

.filter-button {
  background: transparent;
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 10px 14px;
  font: inherit;
}

.filter-button.is-active {
  border-color: var(--cool);
  color: var(--cool);
}

.listen-button,
.speak-card-button {
  background: rgba(255, 255, 255, 0.04);
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 10px 14px;
  font: inherit;
}

.listen-button--secondary {
  color: var(--muted);
}

.listen-status {
  margin: 12px 0 0;
  color: var(--muted);
  min-height: 24px;
}

.band-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.band-head h2 {
  font-size: 1.3rem;
}

.lead-story,
.source-panel {
  min-width: 0;
}

.article-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 18px;
}

.article-grid--compact {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.article-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}

.article-card--featured {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(320px, 0.9fr);
  min-height: 360px;
}

.article-media {
  background: #0d0f12;
  aspect-ratio: 16 / 10;
}

.article-media img {
  height: 100%;
  object-fit: cover;
}

.article-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 18px;
}

.article-title {
  font-size: 1.1rem;
  line-height: 1.35;
}

.article-card--featured .article-title {
  font-size: 1.7rem;
  line-height: 1.15;
}

.article-summary {
  margin: 0;
  color: #d0d4dc;
  line-height: 1.6;
}

.article-tags {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  list-style: none;
  padding: 0;
  margin: auto 0 0;
}

.article-tag {
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 999px;
  padding: 6px 10px;
  color: var(--muted);
  font-size: 0.78rem;
}

.article-actions {
  display: flex;
  gap: 8px;
  margin-top: auto;
}

.source-list {
  display: grid;
  gap: 12px;
}

.source-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px;
  border: 1px solid rgba(255, 255, 255, 0.05);
  border-radius: 8px;
  background: var(--panel-2);
}

.source-name {
  font-size: 0.98rem;
}

.source-count {
  color: var(--green);
  font-size: 1.1rem;
}

[data-hidden="true"] {
  display: none !important;
}

@media (max-width: 1100px) {
  .hero-grid,
  .lead-grid,
  .article-card--featured,
  .article-grid,
  .article-grid--compact {
    grid-template-columns: 1fr;
  }

  .article-grid,
  .article-grid--compact {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 720px) {
  .shell {
    width: min(100vw - 24px, 1200px);
  }

  .hero h1 {
    max-width: none;
  }

  .hero-stats,
  .article-grid,
  .article-grid--compact {
    grid-template-columns: 1fr;
  }
}
"""


SCRIPT = """
const buttons = Array.from(document.querySelectorAll(".filter-button"));
const cards = Array.from(document.querySelectorAll(".article-card"));
const listenButtons = Array.from(document.querySelectorAll(".listen-button"));
const cardListenButtons = Array.from(document.querySelectorAll(".speak-card-button"));
const statusNode = document.querySelector(".listen-status");
const synth = window.speechSynthesis;

function setStatus(message) {
  if (statusNode) {
    statusNode.textContent = message;
  }
}

function stopReading() {
  if (!synth) {
    setStatus("Speech is not supported in this browser.");
    return;
  }
  synth.cancel();
  setStatus("Playback stopped.");
}

function buildSpeechTextFromCard(card) {
  const button = card.querySelector(".speak-card-button");
  if (!button) {
    return "";
  }

  const parts = [
    button.dataset.speechMeta,
    button.dataset.speechTitle,
    button.dataset.speechSummary,
  ].filter(Boolean);

  return parts.join(". ");
}

function speakText(text, label) {
  if (!synth || typeof SpeechSynthesisUtterance === "undefined") {
    setStatus("Speech is not supported in this browser.");
    return;
  }

  const normalized = text.trim();
  if (!normalized) {
    setStatus("Nothing available to read.");
    return;
  }

  synth.cancel();
  const utterance = new SpeechSynthesisUtterance(normalized);
  utterance.rate = 1;
  utterance.pitch = 1;
  utterance.onstart = () => setStatus(`Reading ${label}.`);
  utterance.onend = () => setStatus(`Finished reading ${label}.`);
  utterance.onerror = () => setStatus(`Unable to read ${label}.`);
  synth.speak(utterance);
}

function getVisibleCards() {
  return cards.filter((card) => card.dataset.hidden !== "true");
}

for (const button of buttons) {
  button.addEventListener("click", () => {
    const filter = button.dataset.filter;

    for (const other of buttons) {
      other.classList.toggle("is-active", other === button);
    }

    for (const card of cards) {
      const visible = filter === "all" || card.dataset.group === filter;
      card.dataset.hidden = String(!visible);
    }
  });
}

for (const button of listenButtons) {
  button.addEventListener("click", () => {
    const target = button.dataset.listenTarget;

    if (target === "stop") {
      stopReading();
      return;
    }

    if (target === "lead") {
      const leadCard = document.querySelector(".article-card--featured");
      speakText(leadCard ? buildSpeechTextFromCard(leadCard) : "", "the lead story");
      return;
    }

    const visibleCards = getVisibleCards();
    const text = visibleCards.map(buildSpeechTextFromCard).filter(Boolean).join(". ");
    speakText(text, "the visible stories");
  });
}

for (const button of cardListenButtons) {
  button.addEventListener("click", () => {
    const card = button.closest(".article-card");
    speakText(card ? buildSpeechTextFromCard(card) : "", "the selected story");
  });
}
"""


if __name__ == "__main__":
    main()
