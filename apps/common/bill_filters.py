"""Shared query filters for the bill list endpoints.

The Tally and Zoho list pages render from the same React component and
share one search box ("Search by name, status, uploader…"). Keeping the
translation of that box into a queryset in one place stops the two module
trees from drifting apart on what "search" means.
"""

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
