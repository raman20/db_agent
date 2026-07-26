"""Table-name resolution: one implementation, one ambiguity policy.

This module exists because "resolve what the user typed onto a real catalog key" was implemented
three times, with two *contradictory* ambiguity policies:

* the completer matched on the leaf name via ``rpartition(".")[2]``;
* ``SchemaCache.resolve()`` matched on a dotted suffix and, when several tables matched, returned
  the **alphabetically first**;
* pruning's ``resolve_pinned()`` matched on the leaf and, when several matched, **dropped the
  pin entirely**.

So on a catalog holding both ``sales.orders`` and ``archive.orders``, a single ``@orders`` could
complete happily, resolve to ``archive.orders`` for ``/schema``, and then be silently dropped
before it reached the prompt -- three different answers to one question, none of them visible to
the user.

**The policy chosen here: ambiguity never resolves silently.** :func:`resolve_table_name` returns
``None`` for an ambiguous name exactly as it does for an unknown one, and :func:`match_table_names`
exposes the full candidate list so callers can *say* which tables collided. Guessing the
alphabetically-first table is the worst available option: it is silent, it is arbitrary, and on a
catalog with an ``archive`` schema it reliably picks the stale copy. Dropping silently is only
marginally better -- the user's explicit ``@orders`` pin vanishes with no explanation. Both are
replaced by "ask the user to qualify it".

Deliberately dependency-free (stdlib only) so every layer -- the cache, the completer and the
pruning module -- can import it without a cycle.
"""

from typing import Iterable, List, Optional

#: Quote characters engines wrap identifiers in; stripped before comparison so a pasted
#: `"orders"` or `` `orders` `` resolves like a bare name.
_QUOTES = "\"`'[]"


def bare_name(qualified: str) -> str:
    """The unqualified table name: ``tpch.tiny.orders`` -> ``orders``.

    Splits on the LAST dot, so it is correct for 1-, 2- and 3-part names alike.
    """
    return (qualified or "").rpartition(".")[2]


def normalise(name: str) -> str:
    """Canonical comparison form: unquoted, unprefixed of ``@``, stripped, lowercased."""
    cleaned = (name or "").strip().lstrip("@").strip()
    for quote in _QUOTES:
        cleaned = cleaned.replace(quote, "")
    return cleaned.strip().lower()


def match_table_names(name: str, candidates: Iterable[str]) -> List[str]:
    """All catalog keys a user-typed name could mean, most-specific match first.

    Matching is tried in decreasing specificity and stops at the first tier that produces any
    hit, so a fully-qualified name is never diluted by looser matches:

    1. the exact key (case-insensitively);
    2. a dotted-suffix match -- ``tiny.orders`` matching ``tpch.tiny.orders``;
    3. the leaf name -- ``orders`` matching ``tpch.tiny.orders``.

    Returns ``[]`` when nothing matches, one entry when the name is unambiguous, and several
    (sorted, so callers can render a stable list) when it is ambiguous. Interpreting a
    multi-entry result is the CALLER's job -- see the module docstring.
    """
    needle = normalise(name)
    if not needle:
        return []

    keys = list(candidates)

    exact = [key for key in keys if key.lower() == needle]
    if exact:
        return sorted(exact)

    suffix = [key for key in keys if key.lower().endswith("." + needle)]
    if suffix:
        return sorted(suffix)

    leaf = [key for key in keys if bare_name(key).lower() == needle]
    return sorted(leaf)


def resolve_table_name(name: str, candidates: Iterable[str]) -> Optional[str]:
    """The single table a name means, or ``None`` if it means none or several.

    Ambiguity deliberately returns ``None`` rather than a guess; use
    :func:`match_table_names` when you want to tell the user *which* tables collided.
    """
    matches = match_table_names(name, candidates)
    return matches[0] if len(matches) == 1 else None


def completion_candidates(prefix: str, candidates: Iterable[str]) -> List[str]:
    """Catalog keys whose qualified or leaf name STARTS WITH ``prefix`` (for ``@`` completion).

    Distinct from :func:`match_table_names` on purpose: completion wants prefixes of a
    half-typed word, whereas resolution wants whole names. Sharing the normalisation and
    leaf extraction is what keeps the two consistent.
    """
    needle = normalise(prefix)
    keys = sorted(candidates)
    if not needle:
        return keys
    return [
        key
        for key in keys
        if key.lower().startswith(needle) or bare_name(key).lower().startswith(needle)
    ]
