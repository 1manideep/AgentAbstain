#!/usr/bin/env python
"""Practice-coding tooling (MEMORY_EVOLUTION 7.3, WP8): a blind sample of policy lines, the keyword coder, Cohen's kappa.

    python scripts/code_practices.py sample RUNS_ROOT --n 200 --out sample.csv [--seed 0] [--key KEY] [--salt S]
                                                                                 [--all-versions]
    python scripts/code_practices.py code sample.csv --coder keyword --out codes.csv
    python scripts/code_practices.py kappa a.csv b.csv [--json]

``sample`` draws ``--n`` policy versions from every run under RUNS_ROOT (a directory tree of run directories, each with
a ``world.db``) and writes one row per practice line with the columns ``item_id, policy_id, line_no, text, code``:
no arm, experiment, run, seed or agent column, ids salted hashes, versions in a seeded random order (a version's lines
stay together). ``code`` is left empty for the coder. The mapping back to runs goes to ``--key`` (default: the output
path with ``.key.csv`` in place of ``.csv``); give the coder the sample only. By default each distinct policy text is
drawn at most once (``--all-versions`` keeps duplicates such as the seed text in every run).

``code`` fills the ``code`` column with the deterministic keyword coder of ``docs/research/codebook.md`` and keeps the
other columns. It is the only coder here: the LLM coder of 7.3 is paid and is not built.

``kappa`` joins two coded files on ``item_id`` and prints Cohen's kappa over the items both coded (a code is
compared stripped and upper-cased; a bare digit n is Tn). Items only one file coded are counted and left out.

A codes file (columns ``text`` and ``code``) copied into a run directory as ``practice_codes.csv`` is what the
``cluster_entropy`` outcome reads in place of the keyword coder.

Exit status: 0 on success; 2 when there is nothing to sample or no item both files coded.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from void.research.outcomes.practices import (  # noqa: E402
    KEY_COLUMNS,
    SAMPLE_COLUMNS,
    agreement,
    keyword_code,
    normalize_code,
    sample_policy_lines,
    write_csv,
)

CODERS = ("keyword",)


def _key_path(out: Path) -> Path:
    return out.with_name(out.stem + ".key.csv") if out.suffix == ".csv" else out.with_name(out.name + ".key.csv")


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def cmd_sample(args: argparse.Namespace) -> int:
    out = Path(args.out)
    key = Path(args.key) if args.key else _key_path(out)
    if key.resolve() == out.resolve():
        print("error: --key must differ from --out", file=sys.stderr)
        return 2
    items = sample_policy_lines(args.runs_root, n=args.n, seed=args.seed, salt=args.salt,
                                distinct=not args.all_versions)
    if not items:
        print(f"error: no policy version with a practice line under {args.runs_root}", file=sys.stderr)
        return 2
    write_csv(out, SAMPLE_COLUMNS, (it.blind_row() for it in items))
    write_csv(key, KEY_COLUMNS, (it.key_row() for it in items))
    versions = len({it.policy_id for it in items})
    runs = len({it.run for it in items})
    print(f"sampled {versions} policy versions ({len(items)} lines) from {runs} runs -> {out}; "
          f"key (do not show the coder) -> {key}", file=sys.stderr)
    return 0


def cmd_code(args: argparse.Namespace) -> int:
    columns, rows = _read_rows(Path(args.sample))
    if "text" not in columns or "item_id" not in columns:
        print(f"error: {args.sample} needs 'item_id' and 'text' columns", file=sys.stderr)
        return 2
    if "code" not in columns:
        columns.append("code")
    for row in rows:
        row["code"] = keyword_code(row.get("text") or "")
    write_csv(args.out, columns, rows)
    print(f"coded {len(rows)} lines with the {args.coder} coder -> {args.out}", file=sys.stderr)
    return 0


def _codes(path: Path) -> dict[str, str]:
    columns, rows = _read_rows(path)
    if "item_id" not in columns or "code" not in columns:
        raise SystemExit(f"error: {path} needs 'item_id' and 'code' columns")
    out: dict[str, str] = {}
    for row in rows:
        item = (row.get("item_id") or "").strip()
        if not item:
            continue
        if item in out:
            raise SystemExit(f"error: {path} lists item {item} twice")
        out[item] = normalize_code(row.get("code"))
    return out


def cmd_kappa(args: argparse.Namespace) -> int:
    a, b = _codes(Path(args.a)), _codes(Path(args.b))
    both = sorted(i for i in set(a) & set(b) if a[i] and b[i])
    only = len({i for i in a if a[i]} ^ {i for i in b if b[i]})
    if not both:
        print("error: no item is coded in both files", file=sys.stderr)
        return 2
    result = agreement([a[i] for i in both], [b[i] for i in both])
    if args.json:
        result["confusion"] = [{"a": x, "b": y, "n": n} for (x, y), n in result["confusion"].items()]
        result["coded_in_one_file_only"] = only
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"items coded in both: {result['n']} (coded in one file only: {only})")
    print(f"labels: {', '.join(result['labels'])}")
    print(f"observed agreement: {result['observed']:.4f}")
    print(f"expected agreement: {result['expected']:.4f}")
    print(f"kappa: {result['kappa']:.4f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sample", help="blind sample of policy lines from every run under RUNS_ROOT")
    s.add_argument("runs_root")
    s.add_argument("--n", type=int, default=200, help="policy versions to draw (default 200)")
    s.add_argument("--out", required=True, help="the blind sample CSV for the coder")
    s.add_argument("--key", default=None, help="the key CSV mapping items back to runs (default: OUT with .key.csv)")
    s.add_argument("--seed", type=int, default=0, help="seed of the draw and the order (default 0)")
    s.add_argument("--salt", default=None, help="salt of the hashed ids (default: derived from --seed)")
    s.add_argument("--all-versions", action="store_true", help="draw duplicate texts too (default: each text once)")
    s.set_defaults(fn=cmd_sample)

    c = sub.add_parser("code", help="code a sample CSV's lines")
    c.add_argument("sample")
    c.add_argument("--coder", choices=CODERS, default="keyword")
    c.add_argument("--out", required=True)
    c.set_defaults(fn=cmd_code)

    k = sub.add_parser("kappa", help="Cohen's kappa between two coded CSVs, joined on item_id")
    k.add_argument("a")
    k.add_argument("b")
    k.add_argument("--json", action="store_true", help="print JSON instead of text")
    k.set_defaults(fn=cmd_kappa)

    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
