import argparse
import json
import os
from pathlib import Path
import sys

from .database import Database
from .agent_config import environment
from .embeddings import EmbeddingSpec, SpecterEncoder, run_worker
from .http import HttpClient
from .ingestion import Ingester
from .profile import Profile


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def main(argv=None):
    env = environment()
    parser = argparse.ArgumentParser(description="arXiv metadata → database → asynchronous SPECTER2 embeddings")
    parser.add_argument("--database-url", default=env.get("SHSF_DATABASE_URL"))
    parser.add_argument("--data-dir", default=env.get("SHSF_DATA_DIR", ".data/pipeline"))
    parser.add_argument("--profile", default=env.get("SHSF_PROFILE", "config/robotics.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    web = sub.add_parser("serve", help="Open local seed selection and read-only Zotero import")
    web.add_argument("--port", type=positive, default=8766)
    web.add_argument("--host", default="127.0.0.1")
    agent = sub.add_parser("agent-config", help="Inspect Agent config without exposing secrets")
    agent.add_argument("--test", action="store_true", help="Explicitly send a short connection test")
    agent.add_argument("--task", choices=["explain", "read"], default="explain")
    sub.add_parser("init", help="Apply schema migrations")
    sub.add_parser("status", help="Show persisted data and job progress")
    translation = sub.add_parser("translate-feed", help="Precompute and cache Chinese abstracts for today's recommendations")
    translation.add_argument("--retry-failed", action="store_true", help="Explicitly retry failed or interrupted translations")
    report = sub.add_parser("report", help="Current vector coverage, years, topic coverage and harvest progress")
    report.add_argument("--output", help="Also save a JSON snapshot to this file")
    sub.add_parser("plan", help="Show profile scope; makes no network requests")
    replay = sub.add_parser("replay", help="Parse a saved XML response and ingest it offline")
    replay.add_argument("file")
    replay.add_argument("--force", action="store_true", help="Explicit import outside the admission rules")
    sample = sub.add_parser("fetch", help="Fetch up to 50 explicit arXiv IDs (not yet user seeds)")
    sample.add_argument("ids", nargs="+")
    recent = sub.add_parser("recent", help="Bootstrap a bounded latest-paper snapshot; does not replace OAI history/incremental sync")
    recent.add_argument("--category", default="cs.RO")
    recent.add_argument("--limit", type=positive, default=100)
    harvest = sub.add_parser("harvest", help="Bounded, resumable OAI-PMH harvest")
    harvest.add_argument("--category", default="cs.RO")
    harvest.add_argument("--max-pages", type=positive, default=1)
    harvest.add_argument("--incremental", action="store_true")
    harvest.add_argument("--restart", action="store_true", help="Restart same scope safely using deduplication")
    daily = sub.add_parser("daily-update", help="Sync official announcements, resume initialized OAI scopes, embed and generate daily recommendations")
    daily.add_argument("--max-pages", type=positive, default=50)
    daily.add_argument("--batches", type=positive, default=500)
    daily.add_argument("--batch-size", type=positive, default=8)
    daily.add_argument("--force", action="store_true", help="Rerun ingestion today; saved recommendation batches remain frozen")
    search = sub.add_parser("search", help="Inspect the local metadata with literal keyword search")
    search.add_argument("query")
    search.add_argument("--limit", type=positive, default=10)
    neighbors = sub.add_parser("neighbors", help="Find current-model paper neighbors by cosine similarity; not personal recommendations")
    neighbors.add_argument("arxiv_id")
    neighbors.add_argument("--limit", type=positive, default=10)
    worker = sub.add_parser("embed", help="Process queued embeddings; downloads pinned model on first run")
    worker.add_argument("--batches", type=positive, default=1)
    worker.add_argument("--batch-size", type=positive, default=8)
    worker.add_argument("--device", default="cpu", choices=["cpu", "mps", "cuda"])
    args = parser.parse_args(argv)
    if not args.database_url:
        local_config = Path(args.data_dir) / "database-url"
        args.database_url = local_config.read_text().strip() if local_config.exists() else "sqlite:///.data/pipeline.sqlite3"
    try:
        spec = EmbeddingSpec()
        if args.command == "agent-config":
            from .agent_service import AgentService
            service = AgentService(directory=Path(args.data_dir) / "agent")
            output = service.test(args.task) if args.test else service.status()
        elif args.command == "plan":
            profile = Profile(args.profile)
            output = {"profile": profile.data, "profile_key": profile.fingerprint, "embedding": spec.to_dict(),
                      "embedding_model_key": spec.key, "html_policy": "official HTML only; on demand; no PDF/LaTeX fallback"}
        else:
            db = Database(args.database_url)
            db.migrate()
            if args.command == "serve":
                from .web import serve
                serve(db, args.port, Path(args.data_dir) / "agent", args.profile, host=args.host)
                return 0
            if args.command == "init":
                db.register_model(spec)
                output = db.status()
            elif args.command == "status":
                output = db.status()
            elif args.command == "translate-feed":
                from .agent_service import AgentService
                from .translations import translate_current
                output = translate_current(db, AgentService(directory=Path(args.data_dir) / "agent"), args.data_dir, retry_failed=args.retry_failed)
            elif args.command == "report":
                profile = Profile(args.profile)
                output = db.corpus_report(spec.key, profile.fingerprint)
                if args.output:
                    destination = Path(args.output)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
            elif args.command == "search":
                output = db.search(args.query, args.limit)
            elif args.command == "neighbors":
                from .models import normalize_id
                aid, _ = normalize_id(args.arxiv_id)
                output = {"reference": aid, "model_key": spec.key, "metric": "cosine_similarity",
                          "results": db.neighbors(aid, spec.key, args.limit)}
            elif args.command == "embed":
                output = run_worker(db, spec, SpecterEncoder(spec, Path(args.data_dir) / "models", args.device), args.batches, args.batch_size)
                if output["failed"]:
                    print(json.dumps(output, ensure_ascii=False, indent=2))
                    return 1
            else:
                client = HttpClient(args.data_dir, env.get("SHSF_USER_AGENT", "shsf/0.1 (personal robotics literature research)"))
                ingest = Ingester(db, Profile(args.profile), spec, client, args.data_dir)
                if args.command == "daily-update":
                    from .daily import run_daily
                    output = run_daily(db, ingest, spec, args.data_dir, args.max_pages, args.batches, args.batch_size, force=args.force)
                    if output['status'] in ('completed', 'already_completed'):
                        from .agent_service import AgentService
                        from .translations import translate_current
                        output['translations'] = translate_current(db, AgentService(directory=Path(args.data_dir) / "agent"), args.data_dir)
                elif args.command == "fetch":
                    output = ingest.import_ids(args.ids)
                elif args.command == "recent":
                    output = ingest.import_recent(args.category, args.limit)
                elif args.command == "replay":
                    output = ingest.replay(args.file, args.force)
                else:
                    allowed = ingest.profile.data["core_categories"] + ingest.profile.data["adjacent_categories"]
                    if args.category not in allowed:
                        raise ValueError("Category is not in this profile's configured scope")
                    output = ingest.harvest(args.category, args.max_pages, args.incremental, args.restart)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Committed pages and raw responses are preserved.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Avoid including database connection strings (which can contain secrets).
        message = str(exc).replace(args.database_url, "[database]")
        print(f"Error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
