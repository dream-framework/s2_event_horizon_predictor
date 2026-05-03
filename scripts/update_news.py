#!/usr/bin/env python3
"""Fetch RSS, build stable semantic news topics, fit DREAM S2 decay curves, and publish JSON.

This build adds the missing abstraction layer:
  raw RSS articles -> semantic topic objects with memory -> S2 decay / stickiness.

The implementation intentionally uses only the Python standard library so it runs reliably on
GitHub-hosted Actions without downloading heavy NLP models. It approximates the recommended
embedding/DBSCAN pipeline with sparse TF-IDF vectors, entity/keyword overlap, connected-component
semantic clustering, centroid inertia, and a persistent topic memory file. The output schema remains
compatible with the static frontend.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SOURCES_PATH = ROOT / "scripts" / "sources.json"
HISTORY_PATH = DATA_DIR / "history.json"
OUTPUT_PATH = DATA_DIR / "news_s2.json"
CYCLES_PATH = DATA_DIR / "cycles.json"
TOPIC_MEMORY_PATH = DATA_DIR / "topic_memory.json"

WINDOW_HOURS = 168
BIN_HOURS = 3
MAX_HISTORY_DAYS = 21
MIN_TOPIC_ARTICLES = 3
MIN_CLUSTER_ARTICLES = 3
MIN_FIT_POINTS = 4
MAX_TOPICS = 32
TOPIC_MEMORY_TTL_HOURS = 36
TOPIC_MEMORY_MAX = 600
CENTROID_INERTIA = 0.82
USER_AGENT = "DREAM-S2-NewsDecayBot/0.2 (+https://github.com/)"

# Broad sector labels are still useful as priors and filters, but the dashboard now tracks
# stable semantic clusters inside these sectors rather than only broad keyword buckets.
TOPICS = {
    "ai": {
        "label": "AI / Tech",
        "keywords": ["ai", "artificial intelligence", "openai", "chatgpt", "model", "semiconductor", "chip", "nvidia", "robot", "automation", "software", "machine learning", "data center"],
    },
    "cybersecurity": {
        "label": "Cybersecurity",
        "keywords": ["cyber", "hack", "breach", "ransomware", "malware", "zero-day", "vulnerability", "exploit", "patch", "security", "phishing", "botnet"],
    },
    "quantum": {
        "label": "Quantum tech",
        "keywords": ["quantum", "qubit", "ion trap", "neutral atom", "superconducting", "photonics", "entanglement", "quantum computer", "error correction"],
    },
    "climate": {
        "label": "Climate / Weather",
        "keywords": ["climate", "weather", "storm", "flood", "heat", "wildfire", "emissions", "carbon", "hurricane", "drought", "warming", "temperature", "rain"],
    },
    "markets": {
        "label": "Markets / Economy",
        "keywords": ["market", "stock", "bond", "inflation", "fed", "central bank", "tariff", "trade", "economy", "earnings", "oil", "rate", "currency", "recession", "gdp"],
    },
    "geopolitics": {
        "label": "Geopolitics",
        "keywords": ["war", "military", "ukraine", "russia", "china", "iran", "israel", "gaza", "nato", "sanction", "diplomat", "missile", "defense", "border", "ceasefire", "hostage", "embassy", "foreign minister", "security council", "tariff", "trade war"],
    },
    "public_health": {
        "label": "Public Health",
        "keywords": ["health", "virus", "vaccine", "disease", "hospital", "who", "cancer", "drug", "medicine", "outbreak", "flu", "covid", "clinical", "fda"],
    },
    "space_science": {
        "label": "Space / Science",
        "keywords": ["space", "nasa", "moon", "mars", "telescope", "physics", "science", "research", "planet", "astronomy", "satellite", "starship", "rocket"],
    },
    "space_weather": {
        "label": "Space Weather",
        "keywords": ["solar flare", "geomagnetic", "aurora", "cme", "space weather", "solar storm", "kp index", "coronal mass ejection"],
    },
    "energy": {
        "label": "Energy",
        "keywords": ["energy", "power", "grid", "solar", "wind", "nuclear", "battery", "electric", "gas", "renewable", "fusion", "oil", "lng"],
    },
    "politics": {
        "label": "Politics / Elections",
        "keywords": ["election", "vote", "president", "prime minister", "parliament", "congress", "campaign", "policy", "government", "minister", "court", "senate", "house"],
    },
    "culture_media": {
        "label": "Culture / Media",
        "keywords": ["film", "movie", "music", "streaming", "celebrity", "artist", "tv", "trailer", "festival", "creator", "media", "box office", "game"],
    },
}

STOPWORDS = {
    "about", "above", "after", "again", "against", "all", "also", "amid", "among", "and", "any", "are", "around", "as", "at", "back", "be", "because", "been", "before", "being", "best", "between", "big", "but", "by", "can", "could", "day", "days", "did", "do", "does", "doing", "down", "during", "each", "even", "first", "for", "from", "get", "gets", "go", "goes", "had", "has", "have", "having", "he", "her", "here", "his", "how", "in", "into", "is", "it", "its", "just", "last", "latest", "like", "live", "may", "more", "most", "new", "news", "no", "not", "now", "of", "off", "on", "one", "only", "or", "our", "over", "own", "per", "report", "reports", "says", "she", "should", "so", "some", "than", "that", "the", "their", "them", "then", "there", "these", "they", "this", "those", "through", "time", "to", "today", "under", "up", "update", "updates", "us", "via", "was", "we", "were", "what", "when", "where", "which", "while", "who", "why", "will", "with", "would", "year", "you", "your",
    "www", "http", "https", "com", "org", "net", "html", "rss", "synthetic", "sample", "semantic", "dream", "wire"
}

ENTITY_STOP = {"The", "A", "An", "And", "But", "For", "From", "With", "After", "Before", "This", "That", "These", "Those", "New", "News", "Latest", "Live", "How", "Why", "What", "When", "Where"}


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_dt(value: Optional[str], fallback: dt.datetime) -> dt.datetime:
    if not value:
        return fallback
    value = html.unescape(str(value).strip())
    try:
        iso_value = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(iso_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = dt.datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except ValueError:
            continue
    return fallback


def clean_text(value: Optional[str]) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", str(value))
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def stable_id(*parts: str) -> str:
    raw = "|".join(p.strip() for p in parts if p)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:18]


def canonical_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        filtered = [(k, v) for k, v in query if not k.lower().startswith(("utm_", "fbclid", "gclid"))]
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(filtered), ""))
    except Exception:
        return url


def choose_category(title: str, summary: str) -> Tuple[str, str]:
    text = f"{title} {summary}".lower()
    best_key = "general"
    best_score = 0
    for key, meta in TOPICS.items():
        score = 0
        for kw in meta["keywords"]:
            kw_l = kw.lower()
            if " " in kw_l:
                score += 2 if kw_l in text else 0
            else:
                score += len(re.findall(rf"\b{re.escape(kw_l)}\b", text))
        if score > best_score:
            best_key = key
            best_score = score
    if best_key == "general":
        return "general", "General"
    return best_key, TOPICS[best_key]["label"]


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")


def load_sources() -> List[Dict[str, str]]:
    sources = read_json(SOURCES_PATH, [])
    if not isinstance(sources, list):
        raise ValueError("scripts/sources.json must be a list")
    return sources


def fetch_url(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as response:  # nosec B310: public RSS fetcher
        return response.read()


def tag_text(parent: ET.Element, names: Iterable[str]) -> Optional[str]:
    for name in names:
        found = parent.find(name)
        if found is not None and found.text:
            return found.text
    suffixes = tuple(n.split("}")[-1].split(":")[-1].lower() for n in names)
    for child in parent.iter():
        local = child.tag.split("}")[-1].split(":")[-1].lower()
        if local in suffixes and child.text:
            return child.text
    return None


def parse_feed(xml_bytes: bytes, source: Dict[str, str], fetched_at: dt.datetime) -> List[Dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    items = root.findall(".//item")
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    records: List[Dict[str, Any]] = []
    for item in items[:100]:
        title = clean_text(tag_text(item, ["title", "{http://www.w3.org/2005/Atom}title"]))
        summary = clean_text(tag_text(item, ["description", "summary", "content", "{http://www.w3.org/2005/Atom}summary"]))
        link = tag_text(item, ["link", "guid", "{http://www.w3.org/2005/Atom}link"]) or ""
        if not link:
            for child in item.iter():
                if child.tag.endswith("link") and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        link = clean_text(link)
        if link and not link.startswith("http"):
            link = source.get("home", link)
        link = canonical_url(link)
        published_raw = tag_text(item, ["pubDate", "published", "updated", "dc:date", "{http://www.w3.org/2005/Atom}updated"])
        published_at = parse_dt(published_raw, fetched_at)
        if not title:
            continue
        category, category_label = choose_category(title, summary)
        url_or_title = link or title
        records.append({
            "id": stable_id(source.get("name", "source"), url_or_title, title),
            "title": title,
            "summary": summary[:700],
            "url": link or source.get("home", ""),
            "source": source.get("name", "Unknown"),
            "category": category,
            "category_label": category_label,
            # topic/topic_label are derived later by the semantic tracker. Keep a broad fallback for compatibility.
            "topic": category,
            "topic_label": category_label,
            "published_at": published_at.isoformat(),
            "first_seen_at": fetched_at.isoformat(),
            "last_seen_at": fetched_at.isoformat(),
        })
    return records


def fetch_all(sources: List[Dict[str, str]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    fetched_at = now_utc()
    all_records: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for src in sources:
        url = src.get("url")
        if not url:
            continue
        try:
            payload = fetch_url(url)
            all_records.extend(parse_feed(payload, src, fetched_at))
        except (urllib.error.URLError, ET.ParseError, TimeoutError, OSError) as exc:
            errors.append({"source": src.get("name", url), "error": str(exc)[:240]})
        time.sleep(0.25)
    return all_records, errors


def merge_history(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]], current_time: dt.datetime) -> List[Dict[str, Any]]:
    cutoff = current_time - dt.timedelta(days=MAX_HISTORY_DAYS)
    by_id: Dict[str, Dict[str, Any]] = {}
    for rec in existing + incoming:
        rec_id = rec.get("id") or stable_id(rec.get("source", ""), rec.get("url", ""), rec.get("title", ""))
        rec = dict(rec)
        rec["id"] = rec_id
        pub = parse_dt(rec.get("published_at"), current_time)
        if pub < cutoff or pub > current_time + dt.timedelta(hours=12):
            continue
        category, category_label = choose_category(rec.get("title", ""), rec.get("summary", ""))
        rec.setdefault("category", category)
        rec.setdefault("category_label", category_label)
        if rec_id not in by_id:
            by_id[rec_id] = rec
        else:
            old = by_id[rec_id]
            old_seen = parse_dt(old.get("first_seen_at"), current_time)
            new_seen = parse_dt(rec.get("first_seen_at"), current_time)
            if new_seen < old_seen:
                old["first_seen_at"] = rec.get("first_seen_at")
            old["last_seen_at"] = rec.get("last_seen_at") or rec.get("first_seen_at") or current_time.isoformat()
            for k in ("title", "summary", "url", "source", "category", "category_label", "published_at"):
                if rec.get(k):
                    old[k] = rec[k]
    return sorted(by_id.values(), key=lambda r: r.get("published_at", ""), reverse=True)


# --------------------------- semantic topic layer ---------------------------

def tokenize(text: str) -> List[str]:
    text = clean_text(text).lower()
    raw = re.findall(r"[a-z][a-z0-9_\-]{2,}", text)
    terms = []
    for token in raw:
        token = token.strip("-_")
        if len(token) < 3 or token in STOPWORDS:
            continue
        if token.isdigit():
            continue
        if token.endswith("'s"):
            token = token[:-2]
        # light stemming for English news endings
        if len(token) > 5 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 5 and token.endswith("ing"):
            token = token[:-3]
        elif len(token) > 4 and token.endswith("ed"):
            token = token[:-2]
        elif len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        if token and token not in STOPWORDS:
            terms.append(token)
    return terms


def extract_entities(text: str) -> List[str]:
    text = clean_text(text)
    # Capture simple title-cased entity phrases and all-caps acronyms.
    entity_counter: Counter[str] = Counter()
    for match in re.finditer(r"\b(?:[A-Z][a-zA-Z0-9&.'-]+|[A-Z]{2,})(?:\s+(?:[A-Z][a-zA-Z0-9&.'-]+|[A-Z]{2,})){0,4}\b", text):
        ent = match.group(0).strip(" .,-")
        parts = ent.split()
        if not ent or parts[0] in ENTITY_STOP:
            continue
        if len(ent) < 3:
            continue
        # Avoid sentence-start generic single words.
        if len(parts) == 1 and ent in ENTITY_STOP:
            continue
        entity_counter[ent] += 1
    return [ent for ent, _ in entity_counter.most_common(8)]


def build_idf(term_lists: Sequence[List[str]]) -> Dict[str, float]:
    n = max(1, len(term_lists))
    df: Counter[str] = Counter()
    for terms in term_lists:
        df.update(set(terms))
    return {term: math.log((1 + n) / (1 + count)) + 1.0 for term, count in df.items()}


def normalize_vector(vec: Dict[str, float]) -> Dict[str, float]:
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm <= 0:
        return {}
    return {k: v / norm for k, v in vec.items() if v}


def make_vector(terms: List[str], idf: Dict[str, float], category: str) -> Dict[str, float]:
    tf = Counter(terms)
    vec: Dict[str, float] = {}
    total = max(1, sum(tf.values()))
    for term, count in tf.items():
        vec[term] = (count / total) * idf.get(term, 1.0)
    # Add a low-weight sector prior so completely unrelated sectors do not merge easily.
    if category:
        vec[f"sector::{category}"] = 0.28
    return normalize_vector(vec)


def cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa = set(a)
    sb = set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def centroid(vectors: Sequence[Dict[str, float]]) -> Dict[str, float]:
    acc: Dict[str, float] = defaultdict(float)
    n = 0
    for vec in vectors:
        if not vec:
            continue
        n += 1
        for k, v in vec.items():
            acc[k] += v
    if n <= 0:
        return {}
    return normalize_vector({k: v / n for k, v in acc.items()})


def blend_centroids(old: Dict[str, float], new: Dict[str, float], alpha: float = CENTROID_INERTIA) -> Dict[str, float]:
    if not old:
        return dict(new)
    if not new:
        return dict(old)
    keys = set(old) | set(new)
    return normalize_vector({k: alpha * old.get(k, 0.0) + (1 - alpha) * new.get(k, 0.0) for k in keys})


def top_vector_terms(vec: Dict[str, float], n: int = 10) -> List[str]:
    terms = [(k, v) for k, v in vec.items() if not k.startswith("sector::")]
    return [k for k, _ in sorted(terms, key=lambda kv: kv[1], reverse=True)[:n]]


class DSU:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.size = [1] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]


def article_features(records: List[Dict[str, Any]]) -> Tuple[List[List[str]], List[List[str]], List[Dict[str, float]]]:
    texts = [f"{r.get('title','')} {r.get('summary','')}" for r in records]
    terms = [tokenize(text) for text in texts]
    entities = [extract_entities(text) for text in texts]
    idf = build_idf(terms)
    vectors = [make_vector(t, idf, r.get("category") or r.get("topic") or "general") for r, t in zip(records, terms)]
    return terms, entities, vectors


def connected_semantic_clusters(records: List[Dict[str, Any]], terms: List[List[str]], entities: List[List[str]], vectors: List[Dict[str, float]]) -> List[List[int]]:
    n = len(records)
    if n == 0:
        return []
    dsu = DSU(n)
    term_sets = [set(t[:40]) for t in terms]
    entity_sets = [set(e) for e in entities]
    # Pairwise similarity is acceptable for this small RSS window. It deliberately uses
    # multiple weak signals so wording drift does not fragment real topics.
    for i in range(n):
        for j in range(i + 1, n):
            # Cheap prefilter: no shared terms/entities/sector -> skip most pairs.
            same_sector = (records[i].get("category") == records[j].get("category"))
            term_inter = term_sets[i] & term_sets[j]
            ent_inter = entity_sets[i] & entity_sets[j]
            if not term_inter and not ent_inter and not same_sector:
                continue
            cos = cosine(vectors[i], vectors[j])
            token_j = len(term_inter) / max(1, len(term_sets[i] | term_sets[j]))
            ent_j = len(ent_inter) / max(1, len(entity_sets[i] | entity_sets[j])) if entity_sets[i] or entity_sets[j] else 0.0
            should_link = (
                cos >= 0.30 or
                (same_sector and cos >= 0.24 and len(term_inter) >= 3) or
                (ent_j >= 0.30 and len(ent_inter) >= 1) or
                (token_j >= 0.28 and len(term_inter) >= 3)
            )
            if should_link:
                dsu.union(i, j)
    groups: Dict[int, List[int]] = defaultdict(list)
    for idx in range(n):
        groups[dsu.find(idx)].append(idx)
    clusters = [members for members in groups.values() if len(members) >= MIN_CLUSTER_ARTICLES]
    # Put stronger clusters first.
    clusters.sort(key=lambda idxs: (len(idxs), max(records[i].get("published_at", "") for i in idxs)), reverse=True)
    return clusters


def cluster_keywords(indices: List[int], terms: List[List[str]], n: int = 8) -> List[str]:
    c: Counter[str] = Counter()
    for idx in indices:
        c.update(terms[idx])
    return [term for term, _ in c.most_common(n)]


def cluster_entities(indices: List[int], entities: List[List[str]], n: int = 8) -> List[str]:
    c: Counter[str] = Counter()
    for idx in indices:
        c.update(entities[idx])
    return [ent for ent, _ in c.most_common(n)]


def cluster_category(indices: List[int], records: List[Dict[str, Any]]) -> Tuple[str, str]:
    c = Counter(records[i].get("category") or "general" for i in indices)
    key = c.most_common(1)[0][0]
    label = TOPICS.get(key, {}).get("label") or ("General" if key == "general" else key.replace("_", " ").title())
    return key, label


LABEL_SKIP_TERMS = {
    "news", "latest", "update", "updates", "report", "reports", "says", "said", "new",
    "artificial", "intelligence", "secret", "oscar", "oscars", "scientific", "american",
    "department", "public", "health", "california", "live", "world", "general"
}

def label_tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2 and t not in LABEL_SKIP_TERMS}

def redundant_anchor(candidate: str, anchors: List[str]) -> bool:
    ct = label_tokens(candidate)
    if not ct:
        return True
    for anchor in anchors:
        at = label_tokens(anchor)
        if not at:
            continue
        overlap = len(ct & at) / max(1, min(len(ct), len(at)))
        if ct <= at or at <= ct or overlap >= 0.72:
            return True
    return False

def clean_anchor(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip(" -_/"))
    text = re.sub(r"\b(News|Latest|Update|Updates|Report|Reports)\b", "", text, flags=re.I).strip(" -_/")
    return text[:40].rstrip()

def semantic_label(category_label: str, keywords: List[str], entities: List[str]) -> str:
    anchors: List[str] = []
    # Prefer stable named entities; they survive wording changes better than raw keywords.
    for ent in entities:
        pretty = clean_anchor(ent)
        if pretty and not redundant_anchor(pretty, anchors):
            anchors.append(pretty)
        if len(anchors) >= 2:
            break
    # Add only non-redundant descriptive keywords.
    for kw in keywords:
        raw = kw.replace("_", " ")
        if raw.lower() in LABEL_SKIP_TERMS:
            continue
        pretty = clean_anchor(raw.title() if len(raw) <= 4 else raw)
        if pretty and not redundant_anchor(pretty, anchors):
            anchors.append(pretty)
        if len(anchors) >= 3:
            break
    if anchors:
        label = " / ".join(anchors[:3])
        if len(label) > 54:
            label = label[:51].rstrip() + "..."
        return label
    return category_label


def internal_similarity(indices: List[int], vectors: List[Dict[str, float]]) -> float:
    if len(indices) <= 1:
        return 1.0
    pairs = 0
    total = 0.0
    cap = indices[:80]
    for pos, i in enumerate(cap):
        for j in cap[pos + 1:]:
            total += cosine(vectors[i], vectors[j])
            pairs += 1
    return total / pairs if pairs else 0.0


def next_topic_id(memory: List[Dict[str, Any]]) -> str:
    max_num = 0
    for item in memory:
        key = str(item.get("key", ""))
        m = re.match(r"T(\d+)$", key)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"T{max_num + 1:04d}"


def match_memory(cluster: Dict[str, Any], memory: List[Dict[str, Any]], current_time: dt.datetime) -> Tuple[Optional[Dict[str, Any]], float, Dict[str, float]]:
    best_item: Optional[Dict[str, Any]] = None
    best_score = -1.0
    best_parts = {"vector": 0.0, "keywords": 0.0, "entities": 0.0, "drift": 0.0}
    for item in memory:
        last_seen = parse_dt(item.get("last_seen_at"), current_time)
        age_h = (current_time - last_seen).total_seconds() / 3600.0
        if age_h > TOPIC_MEMORY_TTL_HOURS and not (set(cluster["entities"]) & set(item.get("entities", []))):
            continue
        old_centroid = item.get("centroid") or {}
        vector_score = cosine(cluster["centroid"], old_centroid)
        keyword_score = jaccard(cluster["keywords"], item.get("keywords", []))
        entity_score = jaccard(cluster["entities"], item.get("entities", []))
        # Allow controlled drift: older remembered topics can match with lower vector similarity
        # if the entities or keywords still overlap.
        memory_bonus = min(0.08, float(item.get("history_length") or 0) / 200.0)
        score = 0.62 * vector_score + 0.22 * keyword_score + 0.16 * entity_score + memory_bonus
        if item.get("category") == cluster.get("category"):
            score += 0.04
        if score > best_score:
            best_score = score
            best_item = item
            best_parts = {"vector": vector_score, "keywords": keyword_score, "entities": entity_score, "drift": 1.0 - vector_score}
    if best_item and (best_score >= 0.38 or best_parts["vector"] >= 0.65 or (best_parts["keywords"] >= 0.5 and best_parts["entities"] >= 0.25)):
        return best_item, best_score, best_parts
    return None, best_score, best_parts


def build_semantic_topics(recent: List[Dict[str, Any]], memory: List[Dict[str, Any]], current_time: dt.datetime) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    if not recent:
        return [], memory, [], {"mode": "semantic-lite", "noise_articles": 0, "clusters_seen": 0, "clusters_kept": 0}

    records = [dict(r) for r in recent]
    terms, entities, vectors = article_features(records)
    clusters_idx = connected_semantic_clusters(records, terms, entities, vectors)
    used_indices: set[int] = set()
    semantic_topics: List[Dict[str, Any]] = []
    memory_by_key = {m.get("key"): dict(m) for m in memory if m.get("key")}
    old_keys_seen = set(memory_by_key)

    for indices in clusters_idx[:MAX_TOPICS * 3]:
        vectors_i = [vectors[i] for i in indices]
        cent = centroid(vectors_i)
        keywords = cluster_keywords(indices, terms, 10)
        ents = cluster_entities(indices, entities, 10)
        category, category_label = cluster_category(indices, records)
        sim_internal = internal_similarity(indices, vectors)
        cluster = {
            "centroid": cent,
            "keywords": keywords,
            "entities": ents,
            "category": category,
            "category_label": category_label,
            "article_count": len(indices),
            "internal_similarity": sim_internal,
        }
        old, match_score, match_parts = match_memory(cluster, list(memory_by_key.values()), current_time)
        drift = match_parts.get("drift", 0.0) if old else 0.0
        keyword_stability = match_parts.get("keywords", 1.0) if old else 1.0
        entity_stability = match_parts.get("entities", 1.0) if old else 1.0
        coherence_score = max(0.0, min(1.0, 0.45 * sim_internal + 0.25 * keyword_stability + 0.20 * (1 - min(1, drift)) + 0.10 * entity_stability))
        # This coherence filter is intentionally permissive early, but records the score.
        if len(indices) < MIN_CLUSTER_ARTICLES or coherence_score < 0.12:
            continue
        if old:
            key = old["key"]
            updated_centroid = blend_centroids(old.get("centroid") or {}, cent, CENTROID_INERTIA)
            old_keys_seen.add(key)
            label = old.get("label") or semantic_label(category_label, keywords, ents)
            history_length = int(old.get("history_length") or 0) + 1
            started_at = old.get("started_at") or min(records[i].get("published_at", current_time.isoformat()) for i in indices)
        else:
            key = next_topic_id(list(memory_by_key.values()))
            while key in memory_by_key:
                memory_by_key[key] = memory_by_key[key]
                key = next_topic_id(list(memory_by_key.values()) + [{"key": key}])
            updated_centroid = cent
            label = semantic_label(category_label, keywords, ents)
            history_length = 1
            started_at = min(records[i].get("published_at", current_time.isoformat()) for i in indices)

        matched_records = []
        for idx in indices:
            rec = dict(records[idx])
            rec["topic"] = key
            rec["topic_label"] = label
            rec["semantic_category"] = category
            rec["semantic_category_label"] = category_label
            rec["semantic_terms"] = keywords[:6]
            rec["semantic_entities"] = ents[:6]
            matched_records.append(rec)
            used_indices.add(idx)

        newest = max(parse_dt(r.get("published_at"), current_time) for r in matched_records).isoformat()
        memory_by_key[key] = {
            "key": key,
            "label": label,
            "category": category,
            "category_label": category_label,
            "centroid": updated_centroid,
            "keywords": keywords[:12],
            "entities": ents[:12],
            "started_at": started_at,
            "last_seen_at": newest,
            "history_length": history_length,
            "match_score": round(float(match_score), 6) if old else None,
            "centroid_drift": round(float(drift), 6),
            "keyword_stability": round(float(keyword_stability), 6),
            "entity_stability": round(float(entity_stability), 6),
            "coherence_score": round(float(coherence_score), 6),
            "internal_similarity": round(float(sim_internal), 6),
            "article_count_last": len(matched_records),
        }
        semantic_topics.append({
            "key": key,
            "label": label,
            "category": category,
            "category_label": category_label,
            "records": matched_records,
            "keywords": keywords[:12],
            "entities": ents[:12],
            "coherence_score": coherence_score,
            "centroid_drift": drift,
            "keyword_stability": keyword_stability,
            "entity_stability": entity_stability,
            "internal_similarity": sim_internal,
            "match_score": match_score if old else None,
            "history_length": history_length,
            "topic_memory_key": key,
        })
        if len(semantic_topics) >= MAX_TOPICS:
            break

    # Prune memory, but keep recently seen inactive topics so matching can survive quiet periods.
    pruned_memory = []
    for item in memory_by_key.values():
        last_seen = parse_dt(item.get("last_seen_at"), current_time)
        age_h = (current_time - last_seen).total_seconds() / 3600.0
        if age_h <= MAX_HISTORY_DAYS * 24:
            pruned_memory.append(item)
    pruned_memory.sort(key=lambda item: (item.get("last_seen_at") or "", item.get("history_length") or 0), reverse=True)
    pruned_memory = pruned_memory[:TOPIC_MEMORY_MAX]

    noise = []
    for idx, rec in enumerate(records):
        if idx not in used_indices:
            nr = dict(rec)
            nr["topic"] = "noise"
            nr["topic_label"] = "Noise / unclustered"
            noise.append(nr)
    meta = {
        "mode": "semantic-lite",
        "description": "Sparse TF-IDF vectors + entity/keyword overlap + connected semantic clusters + persistent centroid inertia.",
        "clusters_seen": len(clusters_idx),
        "clusters_kept": len(semantic_topics),
        "noise_articles": len(noise),
        "memory_topics": len(pruned_memory),
        "min_cluster_articles": MIN_CLUSTER_ARTICLES,
        "centroid_inertia": CENTROID_INERTIA,
        "memory_ttl_hours": TOPIC_MEMORY_TTL_HOURS,
    }
    return semantic_topics, pruned_memory, noise, meta


# --------------------------- circadian and S2 layer ---------------------------

def local_activity(hour: float) -> float:
    morning = math.exp(-0.5 * (((hour - 10.5 + 12) % 24 - 12) / 4.2) ** 2)
    afternoon = math.exp(-0.5 * (((hour - 15.5 + 12) % 24 - 12) / 5.0) ** 2)
    return max(0.28, min(1.0, 0.26 + 0.45 * morning + 0.38 * afternoon))


def circadian_factor(when: dt.datetime) -> float:
    when = when.astimezone(dt.timezone.utc)
    utc_hour = when.hour + when.minute / 60
    regional = (
        0.44 * local_activity((utc_hour - 5) % 24) +
        0.36 * local_activity((utc_hour + 1) % 24) +
        0.20 * local_activity((utc_hour + 8) % 24)
    )
    if when.weekday() >= 5:
        regional *= 0.86
    return max(0.35, min(1.05, regional))


def make_bins(records: List[Dict[str, Any]], current_time: dt.datetime) -> Tuple[List[int], List[float], List[float]]:
    n_bins = WINDOW_HOURS // BIN_HOURS
    start = current_time - dt.timedelta(hours=WINDOW_HOURS)
    raw_counts = [0] * n_bins
    corrected_counts = [0.0] * n_bins
    factor_sums = [0.0] * n_bins
    factor_n = [0] * n_bins
    for rec in records:
        published = parse_dt(rec.get("published_at"), current_time)
        if published < start or published > current_time:
            continue
        idx = int((published - start).total_seconds() // 3600 // BIN_HOURS)
        if 0 <= idx < n_bins:
            factor = circadian_factor(published)
            raw_counts[idx] += 1
            corrected_counts[idx] += 1.0 / factor
            factor_sums[idx] += factor
            factor_n[idx] += 1
    bin_factors: List[float] = []
    for i in range(n_bins):
        if factor_n[i]:
            bin_factors.append(factor_sums[i] / factor_n[i])
        else:
            midpoint = start + dt.timedelta(hours=i * BIN_HOURS + BIN_HOURS / 2)
            bin_factors.append(circadian_factor(midpoint))
    return raw_counts, corrected_counts, bin_factors


def circadian_bias(raw_counts: List[int], corrected_counts: List[float]) -> Optional[float]:
    raw_total = sum(raw_counts)
    corr_total = sum(corrected_counts)
    if raw_total <= 0 or corr_total <= 0:
        return None
    raw_norm = [c / raw_total for c in raw_counts]
    corr_norm = [c / corr_total for c in corrected_counts]
    return sum(abs(a - b) for a, b in zip(raw_norm, corr_norm)) / 2.0



# --------------------------- S2 event-horizon layer ---------------------------

SOURCE_TIER_RULES = {
    "mainstream": ["bbc", "npr", "nytimes", "new york times", "guardian", "al jazeera", "cnbc", "politico", "the hill"],
    "specialist": ["nasa", "nature", "sciencedaily", "science daily", "ars technica", "verge", "wired", "technology review", "mit technology"],
    "aggregator": ["google news", "hacker news", "hnrss"],
}


def source_tier(source: Optional[str]) -> str:
    text = (source or "").lower()
    for tier, tokens in SOURCE_TIER_RULES.items():
        if any(tok in text for tok in tokens):
            return tier
    return "niche"


def normalized_entropy(counts: Sequence[int]) -> float:
    total = sum(max(0, int(c)) for c in counts)
    if total <= 0:
        return 0.0
    vals = [c / total for c in counts if c > 0]
    if len(vals) <= 1:
        return 0.0
    ent = -sum(p * math.log(p) for p in vals)
    return max(0.0, min(1.0, ent / math.log(len(vals))))


def horizon_phase(deviation: float, coherence: float, h_index: float, slope: float, observed: Optional[float]) -> str:
    if observed is None:
        return "no data"
    if deviation < 1.08 and coherence < 0.35:
        return "noise"
    if deviation >= 1.18 and coherence < 0.45:
        return "local anomaly"
    if h_index >= 0.92 and slope > 0.015 and coherence >= 0.35 and deviation >= 1.08:
        return "pre-horizon"
    if h_index >= 1.08 and coherence >= 0.55:
        return "post-horizon"
    if h_index >= 0.86 and deviation >= 1.08:
        return "horizon watch"
    return "normal S2 decay"


def horizon_score(deviation: float, coherence: float, h_index: float, slope: float) -> int:
    # Score is deliberately conservative: normal S2 decay stays low; rising, coherent
    # positive deviation is pushed upward.
    score = 0.0
    score += max(0.0, h_index - 0.72) * 58.0
    score += max(0.0, deviation - 1.0) * 23.0
    score += max(0.0, coherence - 0.45) * 22.0
    score += max(0.0, slope) * 140.0
    return int(round(max(0.0, min(100.0, score))))


def apply_event_horizon(fit: Dict[str, Any], records: List[Dict[str, Any]], current_time: dt.datetime, semantic_coherence: float) -> Dict[str, Any]:
    """Annotate series with deviation, cross-scale coherence and S2 event-horizon score.

    H(t) = deviation(t) * cross_scale_coherence(t), where deviation is observed/expected S2.
    The score uses H plus positive slope to separate local bursts from cross-source, rising
    persistence. This is a detection layer, not a replacement for S2 fitting.
    """
    series = fit.get("series") or []
    peak_idx = fit.get("peak_bin_index")
    if peak_idx is None or not series:
        summary = {
            "score": 0,
            "max_score": 0,
            "phase": "warming up",
            "index": None,
            "slope": 0.0,
            "deviation": None,
            "cross_scale_coherence": 0.0,
            "triggered": False,
            "definition": "H = observed/expected_S2 * cross_scale_coherence; alert requires high H and rising H.",
        }
        fit["event_horizon"] = summary
        return summary

    start = current_time - dt.timedelta(hours=WINDOW_HOURS)
    by_x: Dict[int, Dict[str, Any]] = defaultdict(lambda: {"sources": set(), "tiers": Counter(), "articles": 0})
    for rec in records:
        published = parse_dt(rec.get("published_at"), current_time)
        if published < start or published > current_time:
            continue
        bin_idx = int((published - start).total_seconds() // 3600 // BIN_HOURS)
        x_idx = bin_idx - int(peak_idx)
        if x_idx < 0:
            continue
        slot = by_x[x_idx]
        slot["articles"] += 1
        src = rec.get("source") or "Unknown"
        slot["sources"].add(src)
        slot["tiers"][source_tier(src)] += 1

    ema = None
    prev_ema = None
    scored_points = []
    semantic = max(0.0, min(1.0, float(semantic_coherence or 0.0)))
    for point in series:
        x_hours = float(point.get("x_hours") or 0.0)
        x_idx = int(round(x_hours / BIN_HOURS))
        slot = by_x.get(x_idx, {"sources": set(), "tiers": Counter(), "articles": 0})
        tiers = slot["tiers"]
        tier_count = len(tiers)
        source_count = len(slot["sources"])
        tier_span = min(1.0, tier_count / 4.0)
        tier_ent = normalized_entropy(list(tiers.values()))
        source_span = min(1.0, math.log1p(source_count) / math.log1p(8))
        cross = 0.10 + 0.28 * tier_span + 0.22 * tier_ent + 0.22 * source_span + 0.18 * semantic
        cross = max(0.0, min(1.0, cross if slot["articles"] else 0.15 * semantic))

        observed = point.get("observed_corrected", point.get("observed"))
        expected = point.get("fit")
        deviation = None
        if observed is not None and expected is not None:
            deviation = float(observed) / max(0.06, float(expected))
        h_index = None if deviation is None else deviation * cross
        if h_index is not None:
            ema = h_index if ema is None else 0.55 * h_index + 0.45 * ema
            slope = 0.0 if prev_ema is None else ema - prev_ema
            prev_ema = ema
        else:
            slope = 0.0
        phase = horizon_phase(float(deviation or 0.0), cross, float(h_index or 0.0), slope, observed)
        score = horizon_score(float(deviation or 0.0), cross, float(h_index or 0.0), slope)
        point["deviation_ratio"] = None if deviation is None else round(float(deviation), 6)
        point["cross_scale_coherence"] = round(float(cross), 6)
        point["horizon_index"] = None if h_index is None else round(float(h_index), 6)
        point["horizon_slope"] = round(float(slope), 6)
        point["horizon_score"] = score
        point["horizon_phase"] = phase
        point["source_count"] = int(source_count)
        point["source_tier_count"] = int(tier_count)
        scored_points.append(point)

    observed_points = [p for p in scored_points if p.get("horizon_index") is not None]
    latest = observed_points[-1] if observed_points else None
    best = max(observed_points, key=lambda p: p.get("horizon_score") or 0, default=None)
    summary = {
        "score": int(latest.get("horizon_score") or 0) if latest else 0,
        "max_score": int(best.get("horizon_score") or 0) if best else 0,
        "phase": latest.get("horizon_phase") if latest else "warming up",
        "index": latest.get("horizon_index") if latest else None,
        "slope": latest.get("horizon_slope") if latest else 0.0,
        "deviation": latest.get("deviation_ratio") if latest else None,
        "cross_scale_coherence": latest.get("cross_scale_coherence") if latest else 0.0,
        "triggered": bool(latest and latest.get("horizon_phase") in ("pre-horizon", "post-horizon")),
        "best_phase": best.get("horizon_phase") if best else None,
        "definition": "H = observed/expected_S2 * cross_scale_coherence; pre-horizon means H is above normal and rising.",
    }
    fit["event_horizon"] = summary
    return summary


def annotate_article_horizon(article: Dict[str, Any], topic: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(article)
    peak_at = topic.get("peak_at")
    if not peak_at:
        return out
    xh = articleX = 0.0
    try:
        xh = max(0.0, (parse_dt(out.get("published_at"), now_utc()) - parse_dt(peak_at, now_utc())).total_seconds() / 3600.0)
    except Exception:
        return out
    point = nearest_series_point(topic.get("series") or [], xh)
    if point:
        out["age_after_peak_hours"] = round(float(xh), 3)
        out["horizon_score"] = point.get("horizon_score")
        out["horizon_phase"] = point.get("horizon_phase")
        out["horizon_index"] = point.get("horizon_index")
        out["deviation_ratio"] = point.get("deviation_ratio")
        out["cross_scale_coherence"] = point.get("cross_scale_coherence")
    return out

def sse_for_beta_tau(xs: List[float], ys: List[float], tau: float, beta: float) -> float:
    sse = 0.0
    for x, y in zip(xs, ys):
        pred = math.exp(-((max(0.0, x) / tau) ** beta)) if tau > 0 else 0.0
        sse += (y - pred) ** 2
    return sse


def make_series(xs: List[float], ys: List[float], tau: float, beta: float, raw_ys: Optional[List[float]] = None, factors: Optional[List[float]] = None) -> List[Dict[str, float]]:
    out = []
    for i, (x, y) in enumerate(zip(xs, ys)):
        fit = math.exp(-((max(0.0, x) / tau) ** beta)) if tau and tau > 0 else 0.0
        raw_y = raw_ys[i] if raw_ys and i < len(raw_ys) else None
        factor = factors[i] if factors and i < len(factors) else None
        out.append({
            "x_hours": round(float(x), 3),
            "observed": round(float(y), 6),
            "observed_corrected": round(float(y), 6),
            "observed_raw": None if raw_y is None else round(float(raw_y), 6),
            "circadian_factor": None if factor is None else round(float(factor), 6),
            "fit": round(float(fit), 6),
            "residual": round(float(y - fit), 6),
        })
    return out


def empty_fit(series: List[Dict[str, float]]) -> Dict[str, Any]:
    return {
        "tau_hours": None,
        "beta": None,
        "half_life_hours": None,
        "log_r2": None,
        "delta_aic_vs_exp": None,
        "coherence_left_hours": None,
        "series": series,
        "residual_dust": None,
        "phase": "No live signal yet",
        "fit_status": "empty",
        "fit_reason": "No usable articles in the rolling window",
        "peak_bin_index": None,
        "tail_bins": len(series),
    }


def provisional_fit(counts: List[float], peak_idx: int, reason: str, raw_counts: Optional[List[int]] = None, bin_factors: Optional[List[float]] = None) -> Dict[str, Any]:
    if not counts or max(counts) <= 0:
        return empty_fit([])
    peak_count = max(counts) or 1
    tail_counts = counts[peak_idx:] if 0 <= peak_idx < len(counts) else counts
    nonzero_total = sum(1 for c in counts if c > 0)
    active_bins = sum(1 for c in tail_counts if c > 0)
    tau = 18.0 + min(54.0, 3.0 * nonzero_total + 1.5 * math.sqrt(max(0, sum(counts))))
    beta = 1.15 if active_bins <= 2 else 1.35
    if peak_idx >= len(counts) - 2:
        phase = "Collecting post-peak evidence"
    elif sum(tail_counts) < MIN_TOPIC_ARTICLES:
        phase = "Sparse semantic signal"
    else:
        phase = "Provisional S2 guide"
    raw_tail_counts = raw_counts[peak_idx:] if raw_counts else None
    raw_max = max(raw_tail_counts) if raw_tail_counts else 1
    factor_tail = bin_factors[peak_idx:] if bin_factors else None
    series: List[Dict[str, Any]] = []
    horizon_bins = max(8, min(18, len(tail_counts) + 5))
    for i in range(horizon_bins):
        x = i * BIN_HOURS
        obs = tail_counts[i] / peak_count if i < len(tail_counts) else None
        raw_obs = raw_tail_counts[i] / raw_max if raw_tail_counts and i < len(raw_tail_counts) and raw_max > 0 else None
        factor = factor_tail[i] if factor_tail and i < len(factor_tail) else None
        fit = math.exp(-((max(0.0, x) / tau) ** beta))
        residual = None if obs is None else obs - fit
        series.append({
            "x_hours": round(float(x), 3),
            "observed": None if obs is None else round(float(obs), 6),
            "observed_corrected": None if obs is None else round(float(obs), 6),
            "observed_raw": None if raw_obs is None else round(float(raw_obs), 6),
            "circadian_factor": None if factor is None else round(float(factor), 6),
            "fit": round(float(fit), 6),
            "residual": None if residual is None else round(float(residual), 6),
            "projected": obs is None,
        })
    observed_residuals = [abs(float(d["residual"])) for d in series if d.get("residual") is not None]
    residual_dust = math.sqrt(sum(r * r for r in observed_residuals) / max(1, len(observed_residuals))) if observed_residuals else None
    return {
        "tau_hours": tau,
        "beta": beta,
        "half_life_hours": tau * (math.log(2) ** (1 / beta)),
        "log_r2": None,
        "delta_aic_vs_exp": None,
        "coherence_left_hours": tau,
        "series": series,
        "residual_dust": residual_dust,
        "phase": phase,
        "fit_status": "provisional",
        "fit_reason": reason,
        "clock": "published_at",
        "peak_bin_index": peak_idx,
        "tail_bins": len(tail_counts),
    }


def fit_decay(counts: List[float], raw_counts: Optional[List[int]] = None, bin_factors: Optional[List[float]] = None) -> Dict[str, Any]:
    if not counts or max(counts) <= 0:
        return empty_fit([])
    peak_idx = max(range(len(counts)), key=lambda i: counts[i])
    tail_counts = counts[peak_idx:]
    max_count = max(tail_counts) or 1
    ys = [c / max_count for c in tail_counts]
    xs = [i * BIN_HOURS for i in range(len(tail_counts))]
    fit_pairs = [(x, y) for x, y in zip(xs, ys) if y > 0]
    fit_xs = [p[0] for p in fit_pairs]
    fit_ys = [p[1] for p in fit_pairs]
    if len(fit_xs) < MIN_FIT_POINTS or sum(tail_counts) < MIN_TOPIC_ARTICLES:
        reason = f"Need at least {MIN_FIT_POINTS} nonzero post-peak semantic bins and {MIN_TOPIC_ARTICLES} tail articles for a formal S2 fit."
        return provisional_fit(counts, peak_idx, reason, raw_counts, bin_factors)

    tau_values = [4, 6, 8, 10, 12, 16, 20, 24, 30, 36, 42, 48, 60, 72, 90, 108, 132, 156]
    beta_values = [0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 1.0, 1.15, 1.3, 1.5, 1.75, 2.0, 2.25]
    best = (float("inf"), 24.0, 1.0)
    for tau in tau_values:
        for beta in beta_values:
            sse = sse_for_beta_tau(fit_xs, fit_ys, tau, beta)
            if sse < best[0]:
                best = (sse, tau, beta)
    exp_best = (float("inf"), 24.0)
    for tau in tau_values:
        sse = sse_for_beta_tau(fit_xs, fit_ys, tau, 1.0)
        if sse < exp_best[0]:
            exp_best = (sse, tau)

    sse, tau, beta = best
    n = max(1, len(fit_xs))
    eps = 1e-4
    log_y = [math.log(max(eps, y)) for y in fit_ys]
    log_pred = [math.log(max(eps, math.exp(-((max(0.0, x) / tau) ** beta)))) for x in fit_xs]
    mean_log_y = sum(log_y) / n
    sse_log = sum((a - b) ** 2 for a, b in zip(log_y, log_pred))
    sst_log = sum((a - mean_log_y) ** 2 for a in log_y) or 1e-9
    log_r2 = 1 - sse_log / sst_log
    aic_s2 = n * math.log(max(sse / n, 1e-9)) + 2 * 2
    aic_exp = n * math.log(max(exp_best[0] / n, 1e-9)) + 2 * 1
    delta_aic = aic_exp - aic_s2
    half_life = tau * (math.log(2) ** (1 / beta))
    elapsed_since_peak = (len(tail_counts) - 1) * BIN_HOURS
    coherence_left = max(0.0, tau - elapsed_since_peak)
    raw_tail_counts = raw_counts[peak_idx:] if raw_counts else None
    raw_max = max(raw_tail_counts) if raw_tail_counts else 1
    raw_ys = [c / raw_max for c in raw_tail_counts] if raw_tail_counts and raw_max > 0 else None
    factor_tail = bin_factors[peak_idx:] if bin_factors else None
    series = make_series(xs, ys, tau, beta, raw_ys, factor_tail)
    residuals = [d["residual"] for d in series]
    late = residuals[len(residuals)//2:] or residuals
    residual_dust = math.sqrt(sum(float(r) * float(r) for r in late) / max(1, len(late)))
    current_norm = ys[-1] if ys else 0
    if peak_idx >= len(counts) - 2:
        phase = "Collecting post-peak evidence"
    elif current_norm >= 0.55:
        phase = "Active plateau"
    elif current_norm >= 0.18:
        phase = "Cooling"
    else:
        phase = "Residual dust tail"
    return {
        "tau_hours": tau,
        "beta": beta,
        "half_life_hours": half_life,
        "log_r2": log_r2,
        "delta_aic_vs_exp": delta_aic,
        "coherence_left_hours": coherence_left,
        "series": series,
        "residual_dust": residual_dust,
        "phase": phase,
        "fit_status": "formal",
        "fit_reason": "Formal post-peak S2 fit using semantic topic published_at bins",
        "clock": "published_at",
        "peak_bin_index": peak_idx,
        "tail_bins": len(tail_counts),
    }


# --------------------------- cycle archive layer ---------------------------

def verdict_from_fit(fit: Dict[str, Any]) -> str:
    delta = fit.get("delta_aic_vs_exp")
    r2 = fit.get("log_r2")
    if delta is None:
        return "provisional / no formal fit"
    if delta >= 10 and (r2 is None or r2 >= 0.9):
        return "S2 strong"
    if delta >= 6:
        return "S2 likely"
    if delta >= 2:
        return "S2 weak"
    if delta <= -10:
        return "exponential strong"
    if delta <= -2:
        return "exponential likely"
    return "tied / mixed"


def topic_peak_time(topic: Dict[str, Any], output: Dict[str, Any]) -> Optional[dt.datetime]:
    if topic.get("peak_at"):
        return parse_dt(topic.get("peak_at"), now_utc())
    peak_idx = topic.get("peak_bin_index")
    generated_at = output.get("generated_at")
    if peak_idx is None or not generated_at:
        return None
    generated = parse_dt(generated_at, now_utc())
    window_hours = float(output.get("window_hours") or WINDOW_HOURS)
    bin_hours = float(output.get("bin_hours") or BIN_HOURS)
    return generated - dt.timedelta(hours=window_hours) + dt.timedelta(hours=float(peak_idx) * bin_hours)


def nearest_series_point(series: List[Dict[str, Any]], x_hours: float) -> Optional[Dict[str, Any]]:
    if not series:
        return None
    return min(series, key=lambda point: abs(float(point.get("x_hours") or 0) - x_hours))


def story_stickiness(article: Dict[str, Any], topic: Dict[str, Any], peak_at: Optional[dt.datetime]) -> Dict[str, Any]:
    fit = topic.get("fit") or {}
    tau = fit.get("tau_hours")
    series = topic.get("series") or []
    published = parse_dt(article.get("published_at"), peak_at or now_utc())
    x_hours = 0.0
    if peak_at:
        x_hours = max(0.0, (published - peak_at).total_seconds() / 3600.0)
    point = nearest_series_point(series, x_hours)
    observed = None
    expected = None
    residual = 0.0
    if point:
        observed = point.get("observed_corrected", point.get("observed"))
        expected = point.get("fit")
        if observed is not None and expected is not None:
            residual = max(0.0, float(observed) - float(expected))
    post_lambda = bool(tau and x_hours >= float(tau))
    age_weight = 0.45
    if tau and x_hours is not None:
        age_weight = 0.45 + 0.55 * min(1.0, x_hours / max(1e-9, float(tau)))
    post_bonus = 0.18 if post_lambda and residual > 0 else 0.0
    score = int(round(100 * min(1.0, residual * 2.2 * age_weight + post_bonus)))
    if score > 0 and post_lambda:
        role = "post-lambda_q survivor"
    elif score > 0:
        role = "positive S2 residual"
    else:
        role = "decays with baseline"
    return {
        "id": article.get("id"),
        "title": article.get("title"),
        "url": article.get("url"),
        "source": article.get("source"),
        "published_at": article.get("published_at"),
        "stickiness_score": score,
        "residual_contribution": round(float(residual), 6),
        "expected_s2": None if expected is None else round(float(expected), 6),
        "observed_corrected": None if observed is None else round(float(observed), 6),
        "age_after_peak_hours": round(float(x_hours), 3),
        "post_lambda_q": post_lambda,
        "role": role,
        "horizon_score": article.get("horizon_score"),
        "horizon_phase": article.get("horizon_phase"),
        "horizon_index": article.get("horizon_index"),
        "deviation_ratio": article.get("deviation_ratio"),
        "cross_scale_coherence": article.get("cross_scale_coherence"),
    }


def cycle_from_topic(topic: Dict[str, Any], output: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fit = topic.get("fit") or {}
    if fit.get("fit_status") != "formal":
        return None
    peak_at = topic_peak_time(topic, output)
    if not peak_at:
        return None
    generated_at = parse_dt(output.get("generated_at"), now_utc())
    peak_key = peak_at.strftime("%Y%m%dT%H")
    cycle_id = stable_id(topic.get("key", "topic"), peak_key, str(round(float(fit.get("tau_hours") or 0), 2)), str(round(float(fit.get("beta") or 0), 2)))
    articles = [a for a in output.get("articles", []) if a.get("topic") == topic.get("key")]
    sticky = [story_stickiness(a, topic, peak_at) for a in articles]
    sticky.sort(key=lambda item: (item.get("stickiness_score") or 0, item.get("published_at") or ""), reverse=True)
    sticky = sticky[:24]
    max_stickiness = max([s.get("stickiness_score") or 0 for s in sticky], default=0)
    started_at = topic.get("cycle_started_at") or peak_at.isoformat()
    series = topic.get("series") or []
    if series:
        max_x = max(float(p.get("x_hours") or 0) for p in series)
        ended_at = peak_at + dt.timedelta(hours=max_x)
    else:
        ended_at = generated_at
    return {
        "cycle_id": cycle_id,
        "topic": topic.get("key"),
        "topic_label": topic.get("label"),
        "category": topic.get("category"),
        "category_label": topic.get("category_label"),
        "keywords": topic.get("keywords", []),
        "entities": topic.get("entities", []),
        "coherence_score": topic.get("coherence_score"),
        "centroid_drift": topic.get("centroid_drift"),
        "started_at": started_at,
        "peaked_at": peak_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "archived_at": generated_at.isoformat(),
        "article_count": topic.get("article_count", 0),
        "phase": topic.get("phase"),
        "lifecycle_phase": topic.get("phase"),
        "verdict": verdict_from_fit(fit),
        "combined_verdict": f"{verdict_from_fit(fit)} · {topic.get('phase') or 'cycle'}",
        "event_horizon": topic.get("event_horizon", {}),
        "residual_dust": topic.get("residual_dust"),
        "circadian_bias": topic.get("circadian_bias"),
        "peak_bin_index": topic.get("peak_bin_index"),
        "tail_bins": topic.get("tail_bins"),
        "fit": {
            "tau_hours": fit.get("tau_hours"),
            "beta": fit.get("beta"),
            "half_life_hours": fit.get("half_life_hours"),
            "log_r2": fit.get("log_r2"),
            "delta_aic_vs_exp": fit.get("delta_aic_vs_exp"),
            "coherence_left_hours": fit.get("coherence_left_hours"),
            "residual_dust": topic.get("residual_dust"),
        },
        "series": series,
        "sticky_stories": sticky,
        "max_stickiness": max_stickiness,
    }


def norm_cycle_label(label: str) -> str:
    toks = sorted(label_tokens(label))
    return " ".join(toks[:8])

def cycle_score(cycle: Dict[str, Any]) -> float:
    fit = cycle.get("fit") or {}
    h = cycle.get("event_horizon") or {}
    return (
        float(h.get("max_score") or h.get("score") or 0) * 4.0
        + float(cycle.get("max_stickiness") or 0) * 1.2
        + max(0.0, float(fit.get("delta_aic_vs_exp") or 0)) * 3.0
        + max(0.0, float(fit.get("log_r2") or 0)) * 8.0
        + math.log1p(float(cycle.get("article_count") or 0)) * 2.0
    )

def merge_sticky_stories(a: List[Dict[str, Any]], b: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    for story in list(a or []) + list(b or []):
        key = story.get("id") or story.get("url") or story.get("title")
        if not key:
            continue
        old = by_key.get(key)
        if old is None or (story.get("stickiness_score") or 0) > (old.get("stickiness_score") or 0):
            by_key[key] = story
    stories = list(by_key.values())
    stories.sort(key=lambda item: (item.get("stickiness_score") or 0, item.get("published_at") or ""), reverse=True)
    return stories[:24]

def merge_cycle_records(keep: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    # Keep the scientifically stronger representative, but carry forward evidence from the duplicate.
    best, other = (incoming, keep) if cycle_score(incoming) > cycle_score(keep) else (keep, incoming)
    merged = dict(best)
    merged["article_count"] = max(int(keep.get("article_count") or 0), int(incoming.get("article_count") or 0))
    merged["archived_at"] = max(str(keep.get("archived_at") or ""), str(incoming.get("archived_at") or ""))
    merged["duplicate_count"] = int(keep.get("duplicate_count") or 1) + int(incoming.get("duplicate_count") or 1)
    merged["duplicate_cycle_ids"] = sorted(set((keep.get("duplicate_cycle_ids") or []) + (incoming.get("duplicate_cycle_ids") or []) + [keep.get("cycle_id"), incoming.get("cycle_id")]))
    merged["dedupe_reason"] = "same semantic label and nearby peak; retained strongest representative"
    merged["sticky_stories"] = merge_sticky_stories(keep.get("sticky_stories", []), incoming.get("sticky_stories", []))
    merged["max_stickiness"] = max([s.get("stickiness_score") or 0 for s in merged.get("sticky_stories", [])], default=max(keep.get("max_stickiness") or 0, incoming.get("max_stickiness") or 0))
    return merged

def dedupe_cycles(cycles: List[Dict[str, Any]], peak_window_hours: float = 8.0) -> List[Dict[str, Any]]:
    representatives: List[Dict[str, Any]] = []
    for cycle in sorted(cycles, key=cycle_score, reverse=True):
        label = norm_cycle_label(cycle.get("topic_label") or cycle.get("topic") or "")
        peak = parse_dt(cycle.get("peaked_at"), now_utc())
        merged = False
        for idx, existing in enumerate(representatives):
            if label != norm_cycle_label(existing.get("topic_label") or existing.get("topic") or ""):
                continue
            epeak = parse_dt(existing.get("peaked_at"), peak)
            dt_hours = abs((peak - epeak).total_seconds()) / 3600.0
            if dt_hours <= peak_window_hours:
                representatives[idx] = merge_cycle_records(existing, cycle)
                merged = True
                break
        if not merged:
            cycle.setdefault("duplicate_count", 1)
            representatives.append(cycle)
    representatives.sort(key=lambda c: (float((c.get("event_horizon") or {}).get("max_score") or (c.get("event_horizon") or {}).get("score") or 0), c.get("peaked_at") or c.get("archived_at") or ""), reverse=True)
    return representatives

def active_cycle_ids(output: Dict[str, Any]) -> set:
    ids = set()
    if not isinstance(output, dict):
        return ids
    for topic in output.get("topics", []):
        cycle = cycle_from_topic(topic, output)
        if cycle and cycle.get("cycle_id"):
            ids.add(cycle["cycle_id"])
    return ids

def update_cycle_archive(existing_cycles: List[Dict[str, Any]], previous_output: Dict[str, Any], current_output: Dict[str, Any], current_time: dt.datetime) -> List[Dict[str, Any]]:
    # Do not archive the still-active formal cycle. Archive it only when a new wave/peak replaces it.
    active_ids = active_cycle_ids(current_output)
    by_id: Dict[str, Dict[str, Any]] = {}
    for cycle in existing_cycles:
        cid = cycle.get("cycle_id")
        if cid:
            by_id[cid] = cycle
    if isinstance(previous_output, dict):
        for topic in previous_output.get("topics", []):
            cycle = cycle_from_topic(topic, previous_output)
            if not cycle:
                continue
            cid = cycle.get("cycle_id")
            if not cid or cid in active_ids:
                continue
            if cid not in by_id:
                by_id[cid] = cycle
            else:
                by_id[cid] = merge_cycle_records(by_id[cid], cycle)
    cycles = dedupe_cycles(list(by_id.values()))
    # Discovery-first archive: high horizon first, then newest peak, then fit quality.
    cycles.sort(key=lambda c: (float((c.get("event_horizon") or {}).get("max_score") or (c.get("event_horizon") or {}).get("score") or 0), c.get("peaked_at") or c.get("archived_at") or "", cycle_score(c)), reverse=True)
    return cycles[:500]


# --------------------------- output builder ---------------------------

def topic_output_from_semantic(st: Dict[str, Any], current_time: dt.datetime) -> Dict[str, Any]:
    records = st["records"]
    raw_counts, corrected_counts, bin_factors = make_bins(records, current_time)
    fit = fit_decay(corrected_counts, raw_counts, bin_factors)
    bias = circadian_bias(raw_counts, corrected_counts)
    horizon = apply_event_horizon(fit, records, current_time, float(st.get("coherence_score") or 0.0))
    start = current_time - dt.timedelta(hours=WINDOW_HOURS)
    peak_idx = fit.get("peak_bin_index")
    peak_at = start + dt.timedelta(hours=float(peak_idx) * BIN_HOURS) if peak_idx is not None else None
    return {
        "key": st["key"],
        "label": st["label"],
        "category": st.get("category"),
        "category_label": st.get("category_label"),
        "keywords": st.get("keywords", []),
        "entities": st.get("entities", []),
        "coherence_score": round(float(st.get("coherence_score") or 0), 6),
        "centroid_drift": round(float(st.get("centroid_drift") or 0), 6),
        "keyword_stability": round(float(st.get("keyword_stability") or 0), 6),
        "entity_stability": round(float(st.get("entity_stability") or 0), 6),
        "internal_similarity": round(float(st.get("internal_similarity") or 0), 6),
        "history_length": st.get("history_length"),
        "article_count": len(records),
        "phase": fit["phase"],
        "residual_dust": fit["residual_dust"],
        "histogram_counts": [round(float(v), 6) for v in corrected_counts],
        "raw_histogram_counts": raw_counts,
        "circadian_factors": [round(float(v), 6) for v in bin_factors],
        "circadian_bias": None if bias is None else round(float(bias), 6),
        "peak_bin_index": fit.get("peak_bin_index"),
        "tail_bins": fit.get("tail_bins"),
        "peak_at": None if peak_at is None else peak_at.isoformat(),
        "cycle_started_at": None if peak_at is None else peak_at.isoformat(),
        "cycle_ended_at": current_time.isoformat(),
        "fit": {k: fit.get(k) for k in ["tau_hours", "beta", "half_life_hours", "log_r2", "delta_aic_vs_exp", "coherence_left_hours", "fit_status", "fit_reason"]},
        "event_horizon": horizon,
        "series": fit["series"],
    }


def build_output(history: List[Dict[str, Any]], sources: List[Dict[str, str]], errors: List[Dict[str, str]], current_time: dt.datetime, topic_memory: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    cutoff = current_time - dt.timedelta(hours=WINDOW_HOURS)
    recent_raw = [dict(r) for r in history if parse_dt(r.get("published_at"), current_time) >= cutoff]
    semantic_topics, new_memory, noise_records, semantic_meta = build_semantic_topics(recent_raw, topic_memory, current_time)
    topics = [topic_output_from_semantic(st, current_time) for st in semantic_topics]
    topics.sort(key=lambda t: ((t.get("event_horizon") or {}).get("score") or 0, t.get("coherence_score") or 0, t["article_count"], t["residual_dust"] or 0), reverse=True)

    # Only expose articles belonging to coherent semantic topics. Noise remains counted in semantic_meta.
    assigned_articles: List[Dict[str, Any]] = []
    topic_by_key = {t.get("key"): t for t in topics}
    for st in semantic_topics:
        tinfo = topic_by_key.get(st.get("key"))
        for rec in st["records"]:
            assigned_articles.append(annotate_article_horizon(rec, tinfo) if tinfo else rec)
    assigned_articles.sort(key=lambda r: r.get("published_at", ""), reverse=True)

    if not topics:
        fit = empty_fit([])
        raw_counts, corrected_counts, bin_factors = make_bins(recent_raw, current_time)
        topics = [{
            "key": "warming", "label": "Semantic topics warming up", "category": "general", "category_label": "General",
            "keywords": [], "entities": [], "coherence_score": 0, "centroid_drift": None,
            "keyword_stability": None, "entity_stability": None, "internal_similarity": None,
            "history_length": 0, "article_count": len(recent_raw), "phase": "Collecting coherent semantic objects",
            "residual_dust": None,
            "histogram_counts": [round(float(v), 6) for v in corrected_counts],
            "raw_histogram_counts": raw_counts,
            "circadian_factors": [round(float(v), 6) for v in bin_factors],
            "circadian_bias": circadian_bias(raw_counts, corrected_counts),
            "peak_bin_index": None, "tail_bins": 0, "peak_at": None,
            "cycle_started_at": None, "cycle_ended_at": current_time.isoformat(),
            "fit": {k: fit.get(k) for k in ["tau_hours", "beta", "half_life_hours", "log_r2", "delta_aic_vs_exp", "coherence_left_hours", "fit_status", "fit_reason"]},
            "event_horizon": {"score": 0, "max_score": 0, "phase": "warming up", "index": None, "slope": 0.0, "deviation": None, "cross_scale_coherence": 0.0, "triggered": False},
            "series": [],
        }]

    return {
        "generated_at": current_time.isoformat(),
        "window_hours": WINDOW_HOURS,
        "bin_hours": BIN_HOURS,
        "model": {
            "name": "DREAM S2 semantic news-cycle retention",
            "lambda_interpretation": "lambda = elapsed hours since semantic topic attention peak",
            "retention_law": "R(lambda)=exp[-(lambda/lambda_q)^D_eff]",
            "abstraction_layer": "Raw RSS articles are converted into stable semantic topic objects before S2 fitting.",
            "semantic_layer": "Sparse TF-IDF article vectors, entity sets, keyword distributions, semantic connected components, centroid inertia, and a persistent topic_memory.json file.",
            "event_horizon_layer": "H(t)=Deviation(t)*CrossScaleCoherence(t), with Deviation=observed/expected_S2 and CrossScaleCoherence from source-tier spread, source diversity, and semantic coherence. Pre-horizon alerts require high H and positive slope.",
            "signal_preprocessing": "Formal S2 fit uses circadian-corrected publish-time article counts: raw count divided by expected global publishing/activity factor.",
            "observation_clock": "article.published_at, not GitHub Action run time",
            "history_role": "data/history.json retains raw article records and first_seen/last_seen provenance; topic_memory.json retains semantic identity; cycles.json retains completed S2 cycle summaries.",
            "comparison": "Delta AIC = AIC(exponential beta=1) - AIC(stretched S2 beta free)",
            "cycle_archive_role": "data/cycles.json stores deduplicated completed formal cycle summaries so prior S2 learning remains visible when a new wave resets the current topic. Current waves are not archived until replaced by a new peak.",
        },
        "semantic_tracker": semantic_meta,
        "summary": {
            "article_count": len(assigned_articles),
            "raw_recent_count": len(recent_raw),
            "history_count": len(history),
            "source_count": len(sources),
            "fetch_errors": errors,
            "noise_articles": semantic_meta.get("noise_articles", 0),
            "semantic_topic_count": len(semantic_topics),
        },
        "sources": [
            {"name": src.get("name", "Unknown"), "url": src.get("url", ""), "home": src.get("home", "")}
            for src in sources
        ],
        "topics": topics,
        "articles": assigned_articles[:240],
    }, new_memory


# --------------------------- sample data and main ---------------------------

def sample_history(current_time: dt.datetime) -> List[Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    patterns = [
        ("geopolitics", "Ceasefire negotiation update", ["Gaza", "Israel", "Ceasefire"], 60, 30, 1.18, "DREAM Wire"),
        ("ai", "AI model security guidance", ["AI", "OpenAI", "Security"], 36, 18, 1.42, "DREAM Wire"),
        ("cybersecurity", "Critical library patch campaign", ["Patch", "Security", "Exploit"], 48, 24, 1.33, "DREAM Wire"),
        ("culture_media", "Trailer and creator backlash", ["Streaming", "Trailer", "Creator"], 28, 13, 1.96, "DREAM Wire"),
        ("climate", "Severe storm system damage", ["Storm", "Flood", "Weather"], 42, 38, 0.86, "DREAM Wire"),
        ("markets", "Central bank rate-watch", ["Fed", "Inflation", "Markets"], 84, 46, 0.62, "DREAM Wire"),
        ("space_science", "Mars telescope science release", ["NASA", "Mars", "Telescope"], 38, 22, 1.71, "DREAM Wire"),
        ("public_health", "Trial readout and regulator calendar", ["Health", "Trial", "FDA"], 68, 28, 1.25, "DREAM Wire"),
    ]
    for category, base_title, anchors, peak_age_h, tau, beta, source in patterns:
        category_label = TOPICS.get(category, {}).get("label", category)
        for age in range(0, WINDOW_HOURS, 3):
            if age < peak_age_h:
                intensity = max(0.05, age / max(1, peak_age_h))
            else:
                intensity = math.exp(-(((age - peak_age_h) / tau) ** beta))
            repeats = 0
            if intensity > 0.72:
                repeats = 4
            elif intensity > 0.45:
                repeats = 3
            elif intensity > 0.20 and age % 9 == 0:
                repeats = 2
            elif intensity > 0.10 and age % 18 == 0:
                repeats = 1
            for j in range(repeats):
                published = current_time - dt.timedelta(hours=age, minutes=7 * j)
                title = f"{base_title}: {anchors[j % len(anchors)]} update {age:03d}-{j}"
                summary = f"Synthetic semantic sample for {category_label} with anchors {' '.join(anchors)}. Replace by scheduled RSS updates."
                samples.append({
                    "id": stable_id(category, title, published.isoformat()),
                    "title": title,
                    "summary": summary,
                    "url": "",
                    "source": source,
                    "category": category,
                    "category_label": category_label,
                    "topic": category,
                    "topic_label": category_label,
                    "published_at": published.isoformat(),
                    "first_seen_at": published.isoformat(),
                    "last_seen_at": published.isoformat(),
                })
    samples.sort(key=lambda r: r["published_at"], reverse=True)
    return samples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="store_true", help="Generate synthetic sample history instead of fetching RSS.")
    parser.add_argument("--no-fetch", action="store_true", help="Rebuild data from existing history only.")
    args = parser.parse_args()

    current_time = now_utc()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sources = load_sources()
    previous_output = read_json(OUTPUT_PATH, {})
    existing_cycles = read_json(CYCLES_PATH, [])
    if not isinstance(existing_cycles, list):
        existing_cycles = []
    topic_memory = read_json(TOPIC_MEMORY_PATH, [])
    if not isinstance(topic_memory, list):
        topic_memory = []
    existing = read_json(HISTORY_PATH, [])
    if not isinstance(existing, list):
        existing = []

    errors: List[Dict[str, str]] = []
    if args.sample:
        incoming = sample_history(current_time)
        existing = []
        topic_memory = []
    elif args.no_fetch:
        incoming = []
    else:
        incoming, errors = fetch_all(sources)

    history = merge_history(existing, incoming, current_time)
    output, new_topic_memory = build_output(history, sources, errors, current_time, topic_memory)
    cycles = update_cycle_archive(existing_cycles, previous_output, output, current_time)
    output["summary"]["cycle_count"] = len(cycles)
    output["summary"]["topic_memory_count"] = len(new_topic_memory)
    output["cycle_archive"] = {
        "path": "data/cycles.json",
        "cycle_count": len(cycles),
        "latest_cycle_ids": [c.get("cycle_id") for c in cycles[:12]],
    }
    output["topic_memory"] = {
        "path": "data/topic_memory.json",
        "topic_memory_count": len(new_topic_memory),
        "active_memory_keys": [m.get("key") for m in new_topic_memory[:12]],
    }
    write_json(HISTORY_PATH, history)
    write_json(OUTPUT_PATH, output)
    write_json(CYCLES_PATH, cycles)
    write_json(TOPIC_MEMORY_PATH, new_topic_memory)
    print(
        f"Wrote {OUTPUT_PATH.relative_to(ROOT)} with {output['summary']['article_count']} semantic-clustered articles "
        f"across {len(output['topics'])} topics, {len(cycles)} archived cycles, and {len(new_topic_memory)} memory topics."
    )
    if errors:
        print(f"Fetch errors: {len(errors)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
