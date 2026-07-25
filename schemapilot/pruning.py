"""Lexical table pruning: pick the handful of tables a question actually needs.

Why this module exists: the agent used to ship the full DDL of every table (plus three real
data rows each) to a cloud LLM on every question. On a wide catalog that is hundreds of
thousands of tokens per question -- most of it about tables the question never mentions.

Why *lexical* and not embeddings or a table-selection LLM call: both cost either a dependency
(a vector store / model download) or an extra network round trip per question, and the cheap
version already handles the common case -- users name their tables ("how many orders per
customer"). When it misses, the user has two escape hatches that cost nothing: ``@table``
pinning, which force-includes a table, and ``/why``, which prints exactly what was chosen and
with what score. That is why :func:`select_relevant_tables` returns the scores as well as the
picks: a wrong answer has to be debuggable without a debugger.

No LLM call, no embeddings, no new dependency -- ``difflib`` is in the standard library.
"""

import difflib
import re
from typing import Any, Dict, Iterable, List, Optional, Set

from schemapilot.config import settings

#: Words that carry no schema signal. Deliberately grammatical/interrogative only: nouns like
#: "total", "count" or "amount" stay in, because they are extremely common *column* names and
#: dropping them would throw away the strongest hint in questions like "total per invoice".
_STOPWORDS = frozenset({
    "a", "all", "an", "and", "any", "are", "as", "at", "be", "been", "between", "but", "by",
    "can", "did", "do", "does", "each", "find", "for", "from", "get", "give", "had", "has",
    "have", "how", "i", "in", "into", "is", "it", "its", "least", "less", "list", "many",
    "me", "more", "most", "much", "my", "not", "of", "on", "only", "or", "our", "over",
    "please", "show", "some", "than", "that", "the", "their", "them", "then", "there",
    "these", "this", "those", "to", "us", "using", "was", "we", "were", "what", "when",
    "where", "which", "who", "whom", "with", "you", "your",
})

#: A token has to look at least this much like a schema token before it counts as a fuzzy hit.
#: Below ~0.8 difflib starts matching unrelated short words ("user"/"used"/"undo").
_FUZZY_THRESHOLD = 0.82

#: Weights. A table-name hit is worth much more than a column-name hit: "orders" naming a table
#: is near-proof the table is needed, whereas a column called ``order_id`` shows up on half the
#: catalog and is only weak evidence.
_TABLE_TOKEN_WEIGHT = 3.0
_COLUMN_TOKEN_WEIGHT = 1.0
_SCHEMA_TOKEN_WEIGHT = 0.25

#: Slice of the prompt budget held back for foreign-key neighbours (see _expand_one_hop).
_FK_RESERVE_RATIO = 4


def _split_identifier(name: str) -> List[str]:
    """Split ``OrderItems_2024`` / ``order_items`` into lowercase word tokens."""
    # snake_case / kebab-case / dotted first, then camelCase inside each piece.
    pieces = re.split(r"[^0-9A-Za-z]+", name or "")
    tokens: List[str] = []
    for piece in pieces:
        if not piece:
            continue
        tokens.extend(t.lower() for t in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|[0-9]+", piece))
    return tokens


def _singular(token: str) -> str:
    """Crude depluralisation so "customers" matches a ``customer`` table (and vice versa)."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _token_variants(token: str) -> Set[str]:
    return {token, _singular(token)}


def question_tokens(question: str) -> List[str]:
    """Content tokens of the question, deduplicated but order-preserving."""
    seen: Set[str] = set()
    tokens: List[str] = []
    for token in _split_identifier(question):
        if token in _STOPWORDS or len(token) < 2:
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _best_match(question_token: str, schema_tokens: Iterable[str]) -> float:
    """1.0 for an exact (or singular/plural) hit, else the difflib ratio above the threshold."""
    variants = _token_variants(question_token)
    best = 0.0
    for schema_token in schema_tokens:
        if variants & _token_variants(schema_token):
            return 1.0
        ratio = difflib.SequenceMatcher(None, question_token, schema_token).ratio()
        if ratio >= _FUZZY_THRESHOLD and ratio > best:
            best = ratio
    return best


def score_tables(question: str, catalog: Dict[str, Any]) -> Dict[str, float]:
    """Score every table in the catalog against the question. Higher is more relevant."""
    tokens = question_tokens(question)
    tables = catalog.get("tables", {}) or {}
    scores: Dict[str, float] = {}

    for qualified, meta in tables.items():
        # The schema/catalog prefix is scored separately and weakly: "public" matching the word
        # "public" in a question says nothing about relevance.
        parts = qualified.split(".")
        table_tokens = _split_identifier(parts[-1])
        schema_tokens = _split_identifier(".".join(parts[:-1]))
        column_tokens: Set[str] = set()
        for col in meta.get("columns", []) or []:
            column_tokens.update(_split_identifier(col.get("name", "")))

        score = 0.0
        for token in tokens:
            score += _TABLE_TOKEN_WEIGHT * _best_match(token, table_tokens)
            score += _COLUMN_TOKEN_WEIGHT * _best_match(token, column_tokens)
            score += _SCHEMA_TOKEN_WEIGHT * _best_match(token, schema_tokens)

        scores[qualified] = round(score, 3)

    return scores


def resolve_pinned(pinned: Optional[Iterable[str]], catalog: Dict[str, Any]) -> List[str]:
    """Map user-typed ``@table`` names onto real catalog keys.

    The user types what completion offered or what they remember, which may be unqualified
    (``orders``) or differently cased. Resolution order: exact key, case-insensitive key, then
    a match on the unqualified table name. Unresolvable pins are dropped rather than passed
    through, because a name that is not in the catalog cannot be rendered into the prompt.
    """
    tables = catalog.get("tables", {}) or {}
    lowered = {key.lower(): key for key in tables}
    by_leaf: Dict[str, List[str]] = {}
    for key in tables:
        by_leaf.setdefault(key.split(".")[-1].lower(), []).append(key)

    resolved: List[str] = []
    for raw in pinned or []:
        name = (raw or "").strip().lstrip("@")
        if not name:
            continue
        key = name if name in tables else lowered.get(name.lower())
        if key is None:
            candidates = by_leaf.get(name.lower().split(".")[-1], [])
            key = candidates[0] if len(candidates) == 1 else None
        if key and key not in resolved:
            resolved.append(key)
    return resolved


def _adjacency(catalog: Dict[str, Any]) -> Dict[str, List[str]]:
    """Undirected FK graph over catalog table keys."""
    graph: Dict[str, List[str]] = {}
    tables = catalog.get("tables", {}) or {}
    for rel in catalog.get("relationships", []) or []:
        src, dst = rel.get("from_table"), rel.get("to_table")
        if src not in tables or dst not in tables or src == dst:
            continue
        graph.setdefault(src, [])
        graph.setdefault(dst, [])
        if dst not in graph[src]:
            graph[src].append(dst)
        if src not in graph[dst]:
            graph[dst].append(src)
    return graph


def _expand_one_hop(selected: List[str], reasons: Dict[str, str], graph: Dict[str, List[str]],
                    budget: int) -> None:
    """Pull in the direct FK neighbours of the selected tables, in place.

    This is the difference between a query that runs and a query that cannot be written at all.
    "Which customers ordered the most?" scores ``customers`` and ``orders`` highly but a join
    usually has to travel through ``order_items``, whose name the question never mentions. Ship
    the seeds alone and the model either invents a join key or gives up.

    Exactly ONE hop, never a transitive closure: on a normalised schema, two hops from a couple
    of seeds routinely reaches most of the catalog, which re-creates the token blow-up this
    module exists to prevent. One hop is what a single join needs.
    """
    for table in list(selected):
        for neighbour in graph.get(table, []):
            if len(selected) >= budget:
                return
            if neighbour in reasons:
                continue
            selected.append(neighbour)
            reasons[neighbour] = f"foreign-key neighbour of {table}"


def select_relevant_tables(question: str, catalog: Dict[str, Any],
                           pinned: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Choose the tables whose schema is worth spending prompt tokens on.

    Args:
        question: the user's natural-language question.
        catalog: the dict returned by ``db.get_schema_metadata()``.
        pinned: table names the user pinned with ``@table``; always included.

    Returns:
        ``{"tables": [...], "scores": {name: score}, "reasons": {name: why}, "strategy": str}``
        -- the scores and reasons are what ``/why`` prints, so a bad selection is diagnosable.
    """
    tables = catalog.get("tables", {}) or {}
    cap = max(1, int(settings.MAX_PROMPT_TABLES))
    scores = score_tables(question, catalog)

    if not tables:
        return {"tables": [], "scores": {}, "reasons": {}, "strategy": "empty-catalog"}

    forced = resolve_pinned(pinned, catalog)

    # Small database: send everything. Under the cap, pruning can only lose information and
    # saves nothing worth having, so small databases behave exactly as they did before pruning.
    if len(tables) <= cap:
        ordered = sorted(tables, key=lambda t: (-scores.get(t, 0.0), t))
        reasons = {t: ("pinned with @" if t in forced else "small catalog: all tables sent")
                   for t in ordered}
        return {"tables": ordered, "scores": scores, "reasons": reasons,
                "strategy": "full-catalog"}

    ranked = [t for t, s in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])) if s > 0]
    if not ranked:
        # Nothing matched lexically (an acronym-heavy or non-English schema). A bounded,
        # deterministic slice still lets the model answer or say it cannot; an empty schema
        # guarantees a hallucinated table name.
        ranked = sorted(tables)

    selected: List[str] = []
    reasons: Dict[str, str] = {}
    for table in forced:
        selected.append(table)
        reasons[table] = "pinned with @"

    # Hold a slice of the budget back for FK neighbours: filling all 12 slots with lexical hits
    # is how you end up with two ends of a join and no bridge table.
    reserve = max(1, cap // _FK_RESERVE_RATIO) if len(ranked) > cap else 0
    seed_room = max(1, cap - len(selected) - reserve)
    for table in ranked[:seed_room]:
        if len(selected) >= cap:
            break
        if table in reasons:
            continue
        selected.append(table)
        reasons[table] = f"lexical match (score {scores.get(table, 0.0)})"

    _expand_one_hop(selected, reasons, _adjacency(catalog), cap)

    # Any budget the FK hop did not use goes back to the next-best lexical candidates.
    for table in ranked:
        if len(selected) >= cap:
            break
        if table in reasons:
            continue
        selected.append(table)
        reasons[table] = f"lexical match (score {scores.get(table, 0.0)})"

    # Pins are honoured even past the cap: the user named those tables explicitly, which is a
    # stronger signal than any heuristic in this file.
    ordered = sorted(selected, key=lambda t: (-scores.get(t, 0.0), t))
    return {"tables": ordered, "scores": scores, "reasons": reasons, "strategy": "pruned"}
