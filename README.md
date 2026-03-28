## INPROGRESS
- [] testing the coding worklow
- [] simplify the coding workflow

## TODO (MVP 1 - First Market Value)
- [] Add the E2E testing node
- [] Add the lemonsqueezy
- [] Add the memory (using the new research paper instead of RAG I think it based on files)
- [] Add the package auditor node
- [] Add the shadcn skills, main nextjs skill towards the project | try the shadcn cli
- [] Add deploy checklist (add sentry, loggin ) | logging must be implement when building the sandbox (I think)
- [] Add secrets settings


['read_file', 'create_text_file', 'list_dir', 'find_file', 'replace_content', 'delete_lines', 'replace_lines', 'insert_at_line', 'search_for_pattern', 'restart_language_server', 'active_language_server', 'get_symbols_overview', 'find_symbol', 'find_referencing_symbols', 'replace_symbol_body', 'insert_after_symbol', 'insert_before_symbol', 'rename_symbol', 'execute_shell_command']

## editing tools
replace_symbol_body
insert_after_symbol
insert_before_symbol
replace_content
read_file
create_text_file
delete_lines
replace_lines
insert_at_line
restart_language_server
rename_symbol

## research tools
Use symbolic retrieval tools to identify the symbols you need to edit.
You have two main approaches for editing code: (a) editing at the symbol level and (b) file-based editing.
The symbol-based approach is appropriate if you need to adjust an entire symbol, e.g. a method, a class, a function, etc.
It is not appropriate if you need to adjust just a few lines of code within a larger symbol.

You can understand relationships between symbols by using the `find_referencing_symbols` tool. If not explicitly requested otherwise by the user, you make sure that when you edit a symbol, the change is either backward-compatible or you find and update all references as needed.
The `find_referencing_symbols` tool will give you code snippets around the references as well as symbolic information.
You can assume that all symbol editing tools are reliable, so you never need to verify the results if the tools return without error.

list_dir
find_file
search_for_pattern
get_symbols_overview
find_symbol
find_referencing_symbols

---
Create a file upload system with drag-and-drop and progress bar

### Notes
execute sandbox MCP tools in parallel like using asyncio.gather leads to an error