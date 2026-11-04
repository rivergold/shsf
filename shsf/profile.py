import json
from pathlib import Path
import re

from .models import digest, json_text


def words(value):
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def contains(text, term):
    return (" " + words(term) + " ") in (" " + text + " ")


class Profile:
    def __init__(self, path):
        self.data = json.loads(Path(path).read_text())
        self.fingerprint = digest(json_text(self.data))

    def classify(self, paper):
        text = words(paper.title + " " + paper.abstract)
        context = any(contains(text, t) for t in self.data["robot_context"])
        topics = []
        for topic in self.data["topics"]:
            hits = [t for t in topic["terms"] if contains(text, t)]
            if hits and (context or not topic["requires_robot_context"]):
                topics.append({"id": topic["id"], "priority": topic["priority"], "terms": hits})
        core = bool(set(paper.categories) & set(self.data["core_categories"]))
        published = paper.published_at[:10]
        recent = published >= self.data["recent_since"]
        historical = published >= self.data["historical_since"]
        foundation = any(t["id"] == "foundations" for t in topics)
        admitted = (core and recent) or (bool(topics) and historical) or foundation
        reason = "core_recent" if core and recent else "foundation" if foundation else "topic_match" if admitted else "outside_scope"
        priority = min((t["priority"] for t in topics), default=5)
        if not recent:
            priority += 2
        return {"admitted": admitted, "reason": reason, "topics": topics, "priority": priority}
