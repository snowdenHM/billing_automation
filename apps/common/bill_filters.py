"""Shared query filters for the bill list endpoints.

The Tally and Zoho list pages render from the same React component and
share one search box ("Search by name, status, uploader…"). Keeping the
translation of that box into a queryset in one place stops the two module
trees from drifting apart on what "search" means.
"""

import uuid

from django.db.models import Q


def apply_bill_search(queryset, term, name_field="bill_munshi_name"):
    """Filter *queryset* by the list page's single search box.

    Matches the same fields the UI shows in its columns — document name,
    status, uploader and any processing error — so a user searching for
    something visible on screen finds it.

    Args:
        queryset: Bill queryset to narrow.
        term: Raw search string; blank/None returns the queryset untouched.
        name_field: Model field holding the document name. Tally uses
            ``bill_munshi_name``, Zoho uses ``billmunshiName``.

    Returns:
        The filtered queryset. ``distinct()`` is applied because joining
        across ``uploaded_by`` can otherwise repeat rows.
    """
    term = (term or "").strip()
    if not term:
        return queryset

    return queryset.filter(
        Q(**{f"{name_field}__icontains": term})
        | Q(status__icontains=term)
        | Q(processing_error__icontains=term)
        | Q(uploaded_by__first_name__icontains=term)
        | Q(uploaded_by__last_name__icontains=term)
        | Q(uploaded_by__email__icontains=term)
    ).distinct()


def resolve_report_ids(request):
    """Explicit bill ids requested for an export.

    The report endpoints support two modes. Without ids they export every
    bill matching the status filter (the original behaviour, still used by
    any direct/API caller). With ids they export exactly that selection —
    which is what the "Download Excel" button sends once the user has
    ticked rows.

    Accepts a JSON list on a POST body or a comma-separated string on
    either POST or the query string. Sending ids by body keeps a
    hundred-odd UUIDs out of the URL, where they would risk tripping
    proxy request-line limits.

    Returns:
        ``None`` when the caller asked for no particular bills (export
        everything matching status), otherwise a list of validated UUID
        strings — possibly empty, which the caller should treat as
        "nothing selected" rather than "everything".
    """
    raw = None
    if request.method == "POST":
        raw = request.data.get("ids")
    if raw is None:
        raw = request.query_params.get("ids")
    if raw is None:
        return None

    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    else:
        parts = [str(p).strip() for p in raw]

    # Drop anything that isn't a UUID rather than letting it reach the
    # database, where a malformed value raises instead of simply not
    # matching.
    valid = []
    for part in parts:
        if not part:
            continue
        try:
            valid.append(str(uuid.UUID(part)))
        except (ValueError, AttributeError, TypeError):
            continue
    return valid
