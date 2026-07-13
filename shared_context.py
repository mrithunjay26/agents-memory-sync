import argparse

from store import find_project_root, search_shared_context


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    search = subparsers.add_parser("search", help="Search Claude + Codex project context")
    search.add_argument("--project", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    if args.command == "search":
        results = search_shared_context(
            find_project_root(args.project), args.query, limit=args.limit
        )
        if not results:
            print("No shared context matched.")
            return
        for result in results:
            print(
                f"[{result['created_at']}] {result['agent']} "
                f"({result['event_type']}, id={result['id']})"
            )
            print(result["snippet"])
            print()


if __name__ == "__main__":
    main()
