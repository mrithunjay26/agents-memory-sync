import argparse
import json

from repository_intelligence import (
    explain_symbol,
    find_references,
    get_index_status,
    get_repository_map,
    index_repository,
    search_code,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("index", "status"):
        command = commands.add_parser(name)
        command.add_argument("--project", required=True)

    search = commands.add_parser("search")
    search.add_argument("--project", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=10)
    symbol = commands.add_parser("symbol")
    symbol.add_argument("--project", required=True)
    symbol.add_argument("--name", required=True)
    references = commands.add_parser("references")
    references.add_argument("--project", required=True)
    references.add_argument("--name", required=True)
    references.add_argument(
        "--kind", choices=("call", "reference", "tests", "inherits", "import")
    )
    references.add_argument("--limit", type=int, default=100)
    architecture = commands.add_parser("map")
    architecture.add_argument("--project", required=True)
    architecture.add_argument("--directory", default="")
    args = parser.parse_args()

    if args.command == "index":
        result = index_repository(args.project)
    elif args.command == "status":
        result = get_index_status(args.project)
    elif args.command == "search":
        result = search_code(args.project, args.query, limit=args.limit)
    elif args.command == "symbol":
        result = explain_symbol(args.project, args.name)
    elif args.command == "references":
        result = find_references(args.project, args.name, kind=args.kind, limit=args.limit)
    else:
        result = get_repository_map(args.project, directory=args.directory)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
