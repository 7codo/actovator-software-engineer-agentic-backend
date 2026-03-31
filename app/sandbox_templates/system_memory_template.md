# Errors
## Tools Errors
Error handling rules specific to tool usage.

### Symbols Tools Errors
- When find_symbol throws IndexError, assume the symbol exists but has an empty body. Cross-reference with get_symbols_overview to confirm presence — if it's listed there, the symbol is real but unparseable.
- When replace_symbol_body throws InvalidTextLocationError, assume the symbol has an empty body. fall back immediately to insert_after_symbol.