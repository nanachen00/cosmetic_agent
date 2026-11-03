import json

from dotenv import dotenv_values
from neo4j import GraphDatabase


config = dotenv_values("/var/lib/jenkins/tomod/cosmetic-agent/.env")
driver = GraphDatabase.driver(
    config["NEO4J_URI"],
    auth=(config["NEO4J_USER"], config["NEO4J_PASSWORD"]),
)

terms = [
    "每日美妝小知識",
    "皮膚科醫師建議",
    "保養不是步驟越多越好",
    "最近有肌膚問題嗎",
]

sensitive_fragments = (
    "password",
    "secret",
    "token",
    "email",
    "phone",
    "credential",
)


def sanitized(properties):
    return {
        key: value
        for key, value in properties.items()
        if not any(fragment in key.lower() for fragment in sensitive_fragments)
    }


with driver.session() as session:
    labels = session.run(
        """
        MATCH (n)
        UNWIND labels(n) AS label
        RETURN label, count(*) AS count
        ORDER BY label
        """
    ).data()

    matches = session.run(
        """
        MATCH (n)
        WHERE any(
            key IN keys(n)
            WHERE valueType(n[key]) STARTS WITH 'STRING'
              AND any(term IN $terms WHERE n[key] CONTAINS term)
        )
        RETURN elementId(n) AS element_id, labels(n) AS labels, properties(n) AS properties
        ORDER BY labels(n), elementId(n)
        """,
        terms=terms,
    ).data()

print(json.dumps({
    "label_counts": labels,
    "exact_content_matches": [
        {
            "element_id": item["element_id"],
            "labels": item["labels"],
            "properties": sanitized(item["properties"]),
        }
        for item in matches
    ],
}, ensure_ascii=False, default=str, indent=2))

driver.close()
