import json
from collections import Counter

from dotenv import dotenv_values
from neo4j import GraphDatabase


config = dotenv_values("/var/lib/jenkins/tomod/cosmetic-agent/.env")
driver = GraphDatabase.driver(
    config["NEO4J_URI"],
    auth=(config["NEO4J_USER"], config["NEO4J_PASSWORD"]),
)

with driver.session() as session:
    rows = session.run(
        "MATCH (n:FAQEntry) RETURN properties(n) AS properties ORDER BY elementId(n)"
    ).data()

key_counts = Counter()
faq_type_counts = Counter()
status_counts = Counter()
for row in rows:
    properties = row["properties"]
    key_counts.update(properties.keys())
    faq_type_counts[str(properties.get("faq_type", ""))] += 1
    status_counts[str(properties.get("status", ""))] += 1

safe_keys = {
    "question", "answer", "title", "content", "category", "topic",
    "source", "source_url", "url", "updated_at", "published_at",
}

samples = []
for row in rows[:12]:
    properties = row["properties"]
    samples.append({key: properties[key] for key in safe_keys if key in properties})

print(json.dumps({
    "count": len(rows),
    "property_key_counts": dict(key_counts),
    "faq_type_counts": dict(faq_type_counts),
    "status_counts": dict(status_counts),
    "samples": samples,
}, ensure_ascii=False, default=str, indent=2))

driver.close()
