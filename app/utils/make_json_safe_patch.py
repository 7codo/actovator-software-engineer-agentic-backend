import ag_ui_langgraph.utils
from dataclasses import is_dataclass


# 1. The Robust Serializer (Fixes Fix 1)
def make_json_safe_patched(value, _seen=None):
    """
    Handles dataclasses and complex objects without using deepcopy/asdict.
    """
    if _seen is None:
        _seen = set()

    # Handle Primitives
    if isinstance(value, (str, int, float, bool, type(None))):
        return value

    # Prevent Circular References
    obj_id = id(value)
    if obj_id in _seen:
        return "<circular reference>"

    # Handle Dataclasses manually (Avoids deepcopy/asdict)
    if is_dataclass(value) and not isinstance(value, type):
        _seen.add(obj_id)
        try:
            result = {}
            # Manually iterate fields
            for field in value.__dataclass_fields__:
                try:
                    field_value = getattr(value, field)
                    result[field] = make_json_safe_patched(field_value, _seen)
                except Exception:
                    # If a field fails, store its repr
                    result[field] = repr(getattr(value, field, "<error>"))
            return result
        except Exception:
            return repr(value)

    # Handle Dictionaries
    if isinstance(value, dict):
        _seen.add(obj_id)
        return {k: make_json_safe_patched(v, _seen) for k, v in value.items()}

    # Handle Lists/Tuples
    if isinstance(value, (list, tuple)):
        _seen.add(obj_id)
        return [make_json_safe_patched(v, _seen) for v in value]

    # Handle everything else (Fix for itertools.count, RLock, etc.)
    # If json.dumps calls our default function, it means the object is not natively serializable.
    # We must return a serializable representation (string) or raise TypeError.
    try:
        return str(value)
    except Exception:
        return repr(value)


# 2. The Stringify Wrapper (Fixes Fix 2)
def json_safe_stringify_patched(o):
    """
    Used as the 'default' argument for json.dumps.
    It catches non-serializable objects and sanitizes them.
    """
    # We simply delegate to the safe serializer for ALL objects passed here,
    # because json.dumps only passes objects it can't handle.
    return make_json_safe_patched(o)


# 3. Apply Patch
ag_ui_langgraph.utils.make_json_safe = make_json_safe_patched
ag_ui_langgraph.utils.json_safe_stringify = json_safe_stringify_patched