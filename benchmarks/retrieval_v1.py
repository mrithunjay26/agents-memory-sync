from __future__ import annotations


SCENARIOS = (
    ("atlas", "scheduler", "write-ahead journaling", "crash durability"),
    ("beacon", "identity gateway", "short-lived capability tokens", "credential isolation"),
    ("cedar", "billing worker", "idempotency keys", "duplicate charge prevention"),
    ("delta", "event relay", "bounded retry queues", "backpressure safety"),
    ("ember", "notification router", "outbox delivery", "message consistency"),
    ("fjord", "search indexer", "content hash invalidation", "incremental freshness"),
    ("grove", "audit service", "append-only segments", "tamper evidence"),
    ("harbor", "upload coordinator", "multipart checkpoints", "resume reliability"),
    ("iris", "feature service", "snapshot reads", "configuration consistency"),
    ("juniper", "job dispatcher", "lease-based ownership", "worker exclusivity"),
    ("kepler", "metrics collector", "cardinality budgets", "memory stability"),
    ("lumen", "document parser", "streaming tokenization", "bounded memory"),
    ("mesa", "cache warmer", "generation counters", "stale write rejection"),
    ("northstar", "release controller", "two-phase rollout", "rollback safety"),
    ("onyx", "secret broker", "envelope encryption", "key separation"),
    ("prairie", "report builder", "immutable input snapshots", "report reproducibility"),
    ("quartz", "query planner", "cost-based routing", "latency predictability"),
    ("raven", "webhook receiver", "signature-first parsing", "payload authenticity"),
    ("summit", "policy engine", "deny-by-default rules", "authorization safety"),
    ("timber", "artifact registry", "digest-addressed blobs", "artifact integrity"),
    ("umbra", "session manager", "rotating refresh families", "replay resistance"),
    ("vale", "migration runner", "advisory locks", "schema serialization"),
    ("willow", "workflow engine", "durable state transitions", "restart recovery"),
    ("xenon", "rate limiter", "sliding window counters", "burst fairness"),
    ("yarrow", "data exporter", "watermark checkpoints", "incremental completeness"),
    ("zephyr", "edge proxy", "hedged upstream requests", "tail latency control"),
    ("acorn", "tenant catalog", "row-level scopes", "tenant isolation"),
    ("birch", "log compactor", "segment tombstones", "safe reclamation"),
    ("coral", "media pipeline", "content-derived work keys", "render deduplication"),
    ("dune", "backup verifier", "sampled restore drills", "recovery confidence"),
)

CATEGORIES = (
    "exact_lookup",
    "paraphrased_decision_recall",
    "code_navigation",
    "change_localization",
    "dependency_tracing",
    "architecture",
)


def load_dataset() -> dict:
    documents: list[dict] = []
    queries: list[dict] = []

    for number, (slug, subsystem, strategy, quality) in enumerate(SCENARIOS, 1):
        upper = slug.upper()
        title = slug.title()
        incident = f"AMS-{number:03d}-{upper}"
        signal = f"{slug}-signal-{number:03d}"
        function_name = f"load_{slug}_manifest"
        controller = f"{title}Controller"
        pipeline = f"{title}Pipeline"
        repository = f"{title}Repository"
        notifier = f"{title}Notifier"
        gateway = f"{title}Gateway"
        service = f"{title}Service"

        evidence = {
            "exact_lookup": (
                f"Lookup evidence for incident {incident}. The failing artifact is "
                f"config/{slug}-route.yaml and the recovery marker is {signal}."
            ),
            "paraphrased_decision_recall": (
                f"Decision for the {slug} {subsystem}: the team selected {strategy}. "
                f"This protects {quality} when concurrent work overlaps."
            ),
            "code_navigation": (
                f"The function {function_name} is defined in src/{slug}/manifest.py. "
                f"Its focused tests are in tests/{slug}/test_manifest.py."
            ),
            "change_localization": (
                f"Requests showing stale {slug} manifests must be fixed in "
                f"src/{slug}/cache.py; that module owns invalidation after manifest updates."
            ),
            "dependency_tracing": (
                f"{controller} calls {pipeline}, which delegates to {repository} before "
                f"{notifier} publishes completion."
            ),
            "architecture": (
                f"Architecture boundary {slug}-ingress: {gateway} sends validated commands "
                f"to {service}; storage stays behind {repository}."
            ),
        }
        prompts = {
            "exact_lookup": f"Find {incident} and its recovery marker {signal}.",
            "paraphrased_decision_recall": (
                f"Which choice protects {quality} for the {slug} {subsystem}?"
            ),
            "code_navigation": f"Where is {function_name} defined and tested?",
            "change_localization": (
                f"Where should stale {slug} manifest invalidation be changed?"
            ),
            "dependency_tracing": (
                f"What does {controller} call before {repository} is reached?"
            ),
            "architecture": (
                f"Describe the {slug}-ingress flow from {gateway} to storage."
            ),
        }

        for category in CATEGORIES:
            document_id = f"{slug}:{category}"
            query_id = f"q{number:03d}:{category}"
            documents.append(
                {
                    "id": document_id,
                    "agent": "codex" if number % 2 else "claude-code",
                    "event_type": "history",
                    "summary": evidence[category],
                    "text": evidence[category],
                }
            )
            queries.append(
                {
                    "id": query_id,
                    "category": category,
                    "query": prompts[category],
                    "relevance": {document_id: 3},
                }
            )

    return {
        "schema_version": 1,
        "name": "agentmemorysync-retrieval-v1",
        "description": (
            "A synthetic regression suite. It is balanced across six retrieval "
            "categories and must not be used as evidence of real-world or Bluebird parity."
        ),
        "documents": documents,
        "queries": queries,
    }

