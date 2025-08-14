from dataclasses import dataclass
from typing import List, Dict
from waffle import flag_is_active
from waffle.models import Flag
from django.http import HttpRequest


# Define the modules you sell/enable per organization.
MODULES: List[str] = [
    "zoho",
    "tally",
]


def flag_name_for(org_id: int, module: str) -> str:
    """
    Naming convention for per-organization module flags.
    Example: org:12:zoho
    """
    return f"org:{org_id}:{module}"


def list_enabled_modules(request: HttpRequest, org_id: int) -> Dict[str, bool]:
    """
    Check each registered module and return its on/off state for the org.
    We use waffle.flag_is_active on the request, but the flag itself is just
    a simple ON/OFF (everyone) toggle keyed by org+module name.
    """
    results = {}
    for module in MODULES:
        results[module] = flag_is_active(request, flag_name_for(org_id, module))
    return results


def set_module(org_id: int, module: str, active: bool) -> None:
    """
    Upsert a waffle Flag for this org+module. We store a simple 'everyone' toggle.
    """
    if module not in MODULES:
        raise ValueError(f"Unknown module '{module}'. Allowed: {', '.join(MODULES)}")
    name = flag_name_for(org_id, module)
    # Using 'everyone' toggle is the simplest; no need for custom condition sets.
    flag, _ = Flag.objects.get_or_create(name=name, defaults={"everyone": active})
    if flag.everyone != active or flag.disabled is True:
        flag.everyone = active
        flag.disabled = False
        flag.save(update_fields=["everyone", "disabled"])
