"""
repo_locate — which repository does this ticket's work go in, answered by LOOKING.

WHY THIS EXISTS, precisely.

CLC-4555 names two repositories and five files in its own body. `fleet_route` scored it on
hand-written per-repo keywords and came back "tied 2-2 on the word signature", proposing a
repository the ticket never mentions. The session that WROTE that ticket got it right in
seconds — and the only thing it did differently was grep the repos.

That is the whole lesson, and it is not about model quality. The coordinator already had
`repo_grep` and `repo_read` on its belt the entire time. It did not use them, because a tool
named "which repo owns this" reads as an oracle: when a tool exists for a question you call
it and believe the answer. The classifier's existence suppressed the search.

So this is the search, as a tool, so that routing cannot be answered without one having been
run. It is composed in-process by `fleet_route` — not offered as an alternative the model may
skip.

WHAT IT KEYS ON, and why not words.

Keywords fail because English is shared: "signature", "links", "agency" belong to every repo
in a travel platform. IDENTIFIERS are not shared. `b2c_booking_link`, `signatureBooking`,
`odysseus_siid`, `links-data.service.ts` are either in a repository or they are not, and the
answer is a file and a line number rather than a score. A ticket written by anyone technical
is full of them.

HOW A HIT IS READ, which is the part a naive grep gets wrong.

Searching CLC-4555's identifiers across the checkouts puts the most hits for
`b2c_booking_link` in EntityFeatureDataLoaders — and routing there would be WRONG. Those hits
are a vendor swagger file and a CSV export. A mention in documentation says "the reference
material lives here"; a mention in source says "the implementation lives here"; a path that
resolves to a real file says "this exact thing is here". Three different facts, so they are
scored and reported as three different things, and the doc hits come back as something to
attach to the briefing rather than as a destination.

WHAT ABSENCE MEANS. A term found in no repository at all is not a failed search — it is the
finding that the thing does not exist yet. `signatureBooking` appears nowhere, which is how
you know CLC-4555 is greenfield rather than a change to something already written. Reported
explicitly, because a silent zero reads like a broken tool.
"""

import asyncio
import os
import re
import subprocess

import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import git_env, check_repo_access, validate_project_root, ACCESS_LEVELS

log = structlog.get_logger()

# One ls-files and one grep per repository, so the cost is linear in repos and not in
# repos × terms. Everything else is string work in here.
MAX_REPOS = 40
# Separate caps because the two kinds cost differently. A path is matched in memory against
# `git ls-files` output, so twenty of them are free; an identifier becomes a `-e` argument to
# one grep. CLC-4555 names twenty-five paths across two repositories, and a single cap of 16
# dropped the ones that named the SECOND repo — which is the failure this tool exists to fix.
MAX_PATHS = 24
MAX_IDENTS = 16
MAX_TERMS = MAX_PATHS + MAX_IDENTS
GREP_TIMEOUT = 25
MAX_LINES = 4000          # per repo, parsed out of git grep
CONCURRENCY = 8

CODE_EXT = (".ts", ".tsx", ".js", ".jsx", ".vue", ".cs", ".py", ".go", ".rb", ".java",
            ".php", ".sql", ".sh", ".rs", ".kt", ".swift", ".scala", ".c", ".h", ".cpp")
REF_EXT = (".md", ".csv", ".txt", ".rst", ".adoc", ".html")
# A .json or .yaml is source when it configures the app and reference when it documents an
# API. The path says which: a swagger export under a docs directory is not an implementation.
REF_HINTS = ("/docs/", "/doc/", "/claude/", "swagger", "openapi", "readme", "/spec/",
             "postman", "/examples/", "changelog")

_EXT = "ts|tsx|js|jsx|vue|cs|py|go|rb|java|php|sql|sh|rs"
# The extension must END the token. Without the negative lookahead, a markdown table whose
# cells ran together when the body was stored — "links-data.service.tsapps/agency-data-
# services/..." — matches as ONE path of 90 characters that resolves nowhere, and the two
# real files inside it are both lost.
_PATH_TOKEN = re.compile(rf"[\w./-]*\w+\.(?:{_EXT})(?![A-Za-z0-9])")
_RUN_ON = re.compile(rf"(\.(?:{_EXT}))(?=[A-Za-z])")
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}\b")
_CAMEL = re.compile(r"\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]+){1,}\b")
_CONST = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,}\b")
_TICKED = re.compile(r"`([^`\n]{3,60})`")

# Never evidence, whoever matched: generated indexes, vendored trees and build output. A hit
# in .understand-anything/fingerprints.json is a hit in a file that is itself a list of every
# symbol in the repo — it matches everything and therefore distinguishes nothing, and it put
# cenora-agency-links-api on the board as a "source" match for a symbol it does not define.
IGNORE_PATHS = ("node_modules/", "/dist/", "dist/", "/build/", "build/", "coverage/",
                ".understand-anything/", "vendor/", ".venv/", "__pycache__/", ".min.js",
                "-lock.json", ".lock", "/.git/")

# Terms that are in every repository and therefore name none of them. Kept deliberately short:
# over-filtering throws away the distinctive identifier that would have settled the question,
# and a term that is everywhere costs one line of output rather than a wrong answer.
STOP = {
    "package.json", "tsconfig.json", "index.ts", "index.js", "main.ts", "app.ts", "app.js",
    "node_modules", "package-lock.json", "docker-compose.yml", "dockerfile", "readme.md",
    "is_active", "created_at", "updated_at", "deleted_at", "is_deleted", "user_id", "id",
    "api_key", "base_url", "content_type", "application/json", "src", "lib", "dist",
    "get_data", "set_data", "to_string", "on_error", "data_source",
}


def _run(args: list[str], cwd: str, timeout: int = GREP_TIMEOUT) -> str:
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=git_env())
    except (subprocess.TimeoutExpired, OSError):
        return ""
    # git grep exits 1 when nothing matched, which is an answer and not a failure.
    return r.stdout if r.returncode in (0, 1) else ""


def _ignored(path: str) -> bool:
    low = path.lower()
    return any(seg in low for seg in IGNORE_PATHS)


def _is_reference(path: str) -> bool:
    low = path.lower()
    if any(h in low for h in REF_HINTS):
        return True
    if low.endswith(REF_EXT):
        return True
    if low.endswith((".json", ".yaml", ".yml")):
        # Config lives at the root or in config/; a payload sample or an exported spec is
        # reference. Without this every repo's settings file would read as an implementation.
        return not (low.count("/") <= 1 or "/config" in low)
    return False


def extract_terms(text: str, limit: int = MAX_TERMS) -> list[dict]:
    """The identifiers and paths a ticket names, most distinctive first.

    Ranked rather than filtered hard. A term's distinctiveness is approximated by its shape:
    a path is the most specific thing a ticket can say, then a multi-part identifier, then a
    single hump of camelCase. English prose produces none of these shapes, which is the point
    — this never has to decide whether "signature" is a keyword, because it never sees it.
    """
    text = _RUN_ON.sub(r"\1 ", text or "")
    seen: dict[str, dict] = {}

    def add(term: str, kind: str, rank: int):
        t = term.strip().strip(".,:;)(\"'[]{}")
        if len(t) < 4 or t.lower() in STOP:
            return
        prev = seen.get(t.lower())
        if prev is None or rank > prev["rank"]:
            seen[t.lower()] = {"term": t, "kind": kind, "rank": rank}

    # Backticked spans first: the author marked these as code themselves, which is the only
    # explicit signal in the document and beats every heuristic below it.
    for m in _TICKED.finditer(text):
        span = m.group(1).strip()
        if " " in span or "\t" in span:
            continue
        add(span, "path" if _PATH_TOKEN.fullmatch(span) else "ident", 5)

    for m in _PATH_TOKEN.finditer(text):
        add(m.group(0), "path", 4)
    for m in _SNAKE.finditer(text):
        add(m.group(0), "ident", 3 + m.group(0).count("_"))
    for m in _CONST.finditer(text):
        add(m.group(0), "ident", 3)
    for m in _CAMEL.finditer(text):
        humps = sum(1 for c in m.group(0) if c.isupper())
        add(m.group(0), "ident", 2 + humps)

    ordered = sorted(seen.values(), key=lambda d: -d["rank"])
    paths = [d for d in ordered if d["kind"] == "path"][:MAX_PATHS]
    idents = [d for d in ordered if d["kind"] == "ident"][:MAX_IDENTS]
    return (paths + idents)[:limit]


@register_tool
class RepoLocateTool(BaseTool):
    """Find the repository a piece of work belongs in by searching the repositories."""

    @property
    def name(self) -> str:
        return "repo_locate"

    @property
    def description(self) -> str:
        return (
            "WHICH REPOSITORY DOES THIS WORK GO IN — answered by searching the actual "
            "checkouts, not by matching keywords. Give it the ticket text (the whole body, "
            "not a summary) and it pulls out the identifiers and file paths the author "
            "named, searches every repository you may read for each one, and returns the "
            "repositories that carry them with file and line evidence.\n\n"
            "Use it before fleet_route, or let fleet_route call it. An identifier is not "
            "ambiguous the way a keyword is: 'signature' belongs to every repo in a travel "
            "platform, `b2c_booking_link` belongs to whichever one contains it.\n\n"
            "It separates three different facts that a plain search confuses: a path that "
            "RESOLVES to a real file, an identifier in SOURCE, and an identifier only in "
            "DOCUMENTATION — the last one locates the spec to brief with, not the repo to "
            "work in. Terms found in no repository at all are reported too: that is how a "
            "greenfield field is told apart from one that already exists."
        )

    @property
    def short_description(self) -> str:
        return "Find the repo by searching for the ticket's identifiers"

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {
            "text": {"type": "string",
                     "description": "The ticket body, in full. Paste it — a summary strips "
                                    "out exactly the identifiers this searches for"},
            "ticket": {"type": "string",
                       "description": "Ticket key. When the board has the body, it is read "
                                      "from there and `text` can be left out"},
            "terms": {"type": "array", "items": {"type": "string"},
                      "description": "Search these exact strings instead of extracting them"},
            "repos": {"type": "array", "items": {"type": "string"},
                      "description": "Restrict to these repos (owner/repo). Omit to search "
                                     "every repo you may read"},
        }}

    def credential_keys(self) -> list[str]:
        return []

    async def execute(self, text: str = "", ticket: str = "", terms: list = None,
                      repos: list = None, **kw) -> ToolResult:
        body = text or ""
        if ticket and not body:
            body = await self._board_body(ticket)

        if terms:
            wanted = [{"term": str(t), "kind": "path" if _PATH_TOKEN.fullmatch(str(t))
                       else "ident", "rank": 9} for t in terms if str(t).strip()][:MAX_TERMS]
        else:
            wanted = extract_terms(body)

        if not wanted:
            return ToolResult.fail(
                "Nothing searchable in that text. This looks for identifiers and file paths — "
                "`b2c_booking_link`, `links-data.service.ts`, `signatureBooking` — and the "
                "text given has none. Pass the ticket's full body rather than a summary of "
                "it, or pass `terms` explicitly. (A ticket genuinely written without a single "
                "identifier cannot be routed by searching, and should be routed by asking.)")

        candidates = repos or await self._configured_repos()
        if not candidates:
            return ToolResult.fail(
                "No repository is configured for grep access, so there is nothing to search.")

        sem = asyncio.Semaphore(CONCURRENCY)
        unreachable = []

        async def one(repo: str):
            ok, root, access, err, _ = await check_repo_access(repo, "grep", self._profile_slug)
            if not ok:
                unreachable.append({"repo": repo, "why": err})
                return None
            ok, err = validate_project_root(root)
            if not ok:
                unreachable.append({"repo": repo, "why": err})
                return None
            async with sem:
                return await asyncio.to_thread(self._search, repo, root, wanted)

        found = [r for r in await asyncio.gather(*[one(r) for r in candidates[:MAX_REPOS]])
                 if r and r["score"] > 0]
        found.sort(key=lambda r: -r["score"])

        hit_terms = {m["term"] for r in found for m in r["matches"]}
        # Only IDENTIFIERS can prove greenfield. An unresolved path is nearly always the
        # ticket spelling a file a little differently, or naming one it intends to create —
        # reporting those as "does not exist" buries the two or three identifiers where that
        # claim is actually meaningful.
        # A term with a file extension is a file reference however it was classified —
        # markdown cells that ran together produce things like "linkcognito.js" — and a file
        # that did not resolve is a spelling question, not proof that code is unwritten.
        def _filey(t: str) -> bool:
            return bool(re.search(r"\.[A-Za-z]{1,4}$", t))

        missing = [w["term"] for w in wanted
                   if w["term"] not in hit_terms and not _filey(w["term"])]
        unresolved_paths = [w["term"] for w in wanted
                            if w["term"] not in hit_terms and _filey(w["term"])]

        # Where the work goes versus where the spec is. A repo whose every hit is in
        # documentation is not a destination, and offering it as one is how a loader repo
        # ends up owning an API change because it happens to hold the vendor's swagger.
        destinations = [r for r in found if r["source_hits"] or r["path_hits"]]
        references = [{"repo": r["repo"],
                       "files": sorted({m["file"] for m in r["matches"]})[:6],
                       "terms": sorted({m["term"] for m in r["matches"]})}
                      for r in found if not (r["source_hits"] or r["path_hits"])]

        out = {
            "searched": {"repos": len(candidates[:MAX_REPOS]), "terms": [w["term"] for w in wanted]},
            "destinations": [self._public(r) for r in destinations[:6]],
            "reference_material": references[:6],
            "found_nowhere": missing,
            "paths_that_did_not_resolve": unresolved_paths[:10],
            "unreachable": unreachable[:8],
            "verdict": self._verdict(destinations, references, missing),
        }
        log.info("repo_locate", ticket=ticket or None, terms=len(wanted),
                 repos=len(candidates[:MAX_REPOS]), destinations=len(destinations))
        return ToolResult.ok(out)

    # ---- the search ---------------------------------------------------------------

    def _search(self, repo: str, root: str, wanted: list) -> dict:
        """One ls-files and one grep, then all the judgement in Python.

        Deliberately not one call per term: 16 terms over 26 repositories is 416 subprocess
        spawns and a routing decision that takes minutes, which is a decision nobody waits
        for and therefore a tool nobody calls.
        """
        matches, score = [], 0
        path_terms = [w["term"] for w in wanted if w["kind"] == "path"]
        ident_terms = [w["term"] for w in wanted if w["kind"] == "ident"]

        # A path that resolves to a tracked file is the strongest thing a search can say: the
        # ticket named a file and the file is here. No scoring judgement is involved.
        tracked = _run(["git", "ls-files"], root).splitlines() if path_terms else []
        by_base = {}
        for p in tracked:
            by_base.setdefault(os.path.basename(p).lower(), []).append(p)
        path_hits = []
        for term in path_terms:
            base = os.path.basename(term).lower()
            cands = [r for r in by_base.get(base, []) if not _ignored(r)]
            # A path given with directories must agree with the one on disk, or `post.js`
            # claims every repo that happens to have one. Matched on the LAST TWO segments,
            # not the whole string: tickets write the same file as `links/post.js`,
            # `controllers/links/post.js` and `cenora-agency-links-api/src/.../post.js`, and
            # only the tail is common to all three.
            #
            # Filtered BEFORE truncating. Taking the first three candidates and then checking
            # agreement discarded `controllers/links/post.js` — a file that exists — because
            # three other post.js files sorted ahead of it, and it was then reported as
            # "found in no repository", i.e. as greenfield work.
            tail = "/".join(term.replace("\\", "/").lower().split("/")[-2:]) if "/" in term else ""
            if tail:
                cands = [r for r in cands if r.lower().endswith(tail)]
            for real in cands[:3]:
                path_hits.append({"term": term, "file": real})
                matches.append({"term": term, "file": real, "line": 0, "where": "path"})
                score += 10

        source_hits = ref_hits = 0
        if ident_terms:
            args = ["git", "grep", "-n", "-I", "-F", "--max-count", "3"]
            for t in ident_terms:
                args += ["-e", t]
            seen_pairs = set()
            for line in _run(args, root).splitlines()[:MAX_LINES]:
                # path:line:content — a path may contain colons, so split from the left twice
                # only after finding the line number.
                m = re.match(r"^(.*?):(\d+):(.*)$", line)
                if not m:
                    continue
                path, lineno, content = m.group(1), int(m.group(2)), m.group(3).strip()
                if _ignored(path):
                    continue
                for t in ident_terms:
                    if t not in content:
                        continue
                    ref = _is_reference(path)
                    pair = (t, ref)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    matches.append({"term": t, "file": path, "line": lineno,
                                    "where": "reference" if ref else "source",
                                    "text": content[:160]})
                    if ref:
                        ref_hits += 1
                        score += 1
                    else:
                        source_hits += 1
                        score += 4
        return {"repo": repo, "score": score, "matches": matches[:24],
                "path_hits": len(path_hits), "source_hits": source_hits,
                "reference_hits": ref_hits}

    @staticmethod
    def _public(r: dict) -> dict:
        return {"repo": r["repo"], "score": r["score"],
                "paths_resolved": r["path_hits"], "in_source": r["source_hits"],
                "in_docs": r["reference_hits"],
                "evidence": [m for m in r["matches"] if m["where"] != "reference"][:8]
                            or r["matches"][:4]}

    @staticmethod
    def _verdict(destinations: list, references: list, missing: list) -> str:
        parts = []
        if not destinations:
            parts.append(
                "No repository contains any of these identifiers in source. That is a real "
                "finding and not an empty search: either the work is genuinely new everywhere, "
                "or the ticket is written in prose the author never turned into names. Ask "
                "rather than falling back to keywords.")
        else:
            top = destinations[0]
            second = destinations[1] if len(destinations) > 1 else None
            lead = (f"{top['repo']} — {top['path_hits']} of the named files resolve there and "
                    f"{top['source_hits']} identifier(s) appear in its source")
            if second and second["score"] >= top["score"] * 0.6:
                parts.append(
                    f"Two repositories carry this work: {lead}; and {second['repo']} "
                    f"({second['path_hits']} paths, {second['source_hits']} in source). A "
                    f"ticket whose identifiers live in two places usually needs a unit in "
                    f"each, in dependency order — do not pick one and drop the other.")
            else:
                parts.append(f"The work belongs in {lead}. This is measured, not scored: "
                             f"open the files listed and check.")
        if references:
            parts.append(
                "Reference material (documentation hits only, NOT where the work goes): " +
                "; ".join(f"{r['repo']} → {', '.join(r['files'][:2])}" for r in references[:3]) +
                ". Attach it to the unit — it is what the dev would otherwise be missing.")
        if missing:
            parts.append(
                f"Found in no repository at all: {', '.join(missing[:8])}. These do not exist "
                f"yet, so that part of the ticket is new code rather than a change.")
        return " ".join(parts)

    # ---- inputs -------------------------------------------------------------------

    async def _board_body(self, ticket: str) -> str:
        """The ticket body, if the fleet's board happens to hold it.

        Guarded by to_regclass rather than try/except: this skill must load and work in an
        executor where the fleet skill is not installed, and a failed statement would also
        poison any transaction around it.
        """
        from ...db import get_pool
        pool = get_pool()
        if not pool:
            return ""
        key = (ticket or "").strip().upper()
        if not key:
            return ""
        async with pool.acquire() as conn:
            present = await conn.fetchval(
                "SELECT to_regclass('public.fleet_tickets') IS NOT NULL")
            if not present:
                return ""
            return await conn.fetchval(
                """SELECT left(body, 40000) FROM fleet_tickets
                    WHERE parent_key = $1 AND body_fetched""", key) or ""

    async def _configured_repos(self) -> list[str]:
        from ...db import get_pool
        pool = get_pool()
        if not pool:
            return []
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT repo, access FROM skill_repo_config WHERE enabled ORDER BY repo")
        return [r["repo"] for r in rows
                if "grep" in ACCESS_LEVELS.get(r["access"], set())]
