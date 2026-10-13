import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pgrag",
        description="Project Gorgon knowledge base: fetch data, build documents and the tool corpus.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("download-cdn", help="Fetch CDN JSON exports to data/cdn/")
    p.set_defaults(func=_download_cdn)

    p = sub.add_parser("download-wiki", help="Fetch wiki pages to data/wiki/ (slow)")
    p.set_defaults(func=_download_wiki)

    p = sub.add_parser("build-documents", help="Generate data/documents.json from CDN + wiki")
    p.set_defaults(func=_build_documents)

    args = parser.parse_args()
    rc = args.func(args)
    sys.exit(rc if isinstance(rc, int) else 0)


def _download_cdn(args) -> int:
    from pgrag.loaders.download_cdn import download_cdn

    download_cdn()
    return 0


def _download_wiki(args) -> int:
    from pgrag.loaders.download_wiki import main as sync_wiki

    return sync_wiki()


def _build_documents(args) -> int:
    from pgrag.build import generate_documents

    generate_documents()
    return 0


if __name__ == "__main__":
    main()
