from langchain_core.messages import AIMessage, ToolMessage
from colorama import Fore, Style, init

init(autoreset=True)

async def interactive_graph(graph, is_use_google_models=False):
    async def _run_workflow(messages):
        result = await graph.ainvoke(
            {
                "messages": messages,
            }
        )
        return result.get("messages", messages)
    print(f"{Fore.CYAN}{Style.BRIGHT}Interactive workflow started. Type 'exit' to quit.{Style.RESET_ALL}")
    messages = []
    while True:
        try:
            print("\n\n","-"*6,"\n\n",)
            user_input = input(f"{Fore.WHITE}{Style.BRIGHT}You: {Style.RESET_ALL}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{Fore.CYAN}Exiting.{Style.RESET_ALL}")
            break
        if user_input.lower() in {"exit", "quit"}:
            print(f"{Fore.CYAN}Goodbye.{Style.RESET_ALL}")
            break
        if not user_input:
            continue
        messages.append(
            {
                "role": "user",
                "content": user_input,
            }
        )
        try:
            previous_message_count = len(messages)
            
            messages = await _run_workflow(messages)
            print(messages)
            new_messages = messages[previous_message_count:]
            # Print all assistant messages
            assistant_messages: list[AIMessage] = [
                m for m in new_messages if isinstance(m, AIMessage)
            ]
            tools_messages: list[ToolMessage] = [
                m for m in new_messages if isinstance(m, ToolMessage)
            ]
            
            assistant_messages_with_tool_calls: list[AIMessage] = [
                m
                for m in new_messages
                if isinstance(m, AIMessage)
                and hasattr(m, "tool_calls")
                and len(m.tool_calls) > 0
            ]

            # Print all assistant-to-tool calls
            if assistant_messages_with_tool_calls:
                print(f"{Fore.YELLOW}{Style.BRIGHT}=== Tool Calls ==={Style.RESET_ALL}")
                for am in assistant_messages_with_tool_calls:
                    print(am)
                    for tool_call in am.tool_calls:
                        print(
                            f"{Fore.CYAN}Tool Call Name: {Fore.WHITE}{tool_call['name']}{Fore.CYAN}, Args: {Fore.WHITE}{tool_call['args']}{Style.RESET_ALL}"
                        )

            # Print all assistant messages
            if assistant_messages:
                print(f"{Fore.GREEN}{Style.BRIGHT}=== Assistant Messages ==={Style.RESET_ALL}")
                for am in assistant_messages:
                    print(am)
                    # if isinstance(am.content, list) and len(am.content) > 0: ## For google models
                    if is_use_google_models and len(am.content) > 0:
                        print(f"{Fore.GREEN}Assistant: {Fore.WHITE}{am.content[0]['text']}{Style.RESET_ALL}")
                    else:
                        print(f"{Fore.GREEN}Assistant: {Fore.WHITE}{am.content}{Style.RESET_ALL}")

            # Print all tool messages
            if tools_messages:
                print(f"{Fore.MAGENTA}{Style.BRIGHT}=== Tool Messages ==={Style.RESET_ALL}")
                for tm in tools_messages:
                    print(tm)
                    if is_use_google_models and len(tm.content) > 0:
                        # Fix: Defensive handling for non-dict, non-list or unexpected tm.content types
                        tool_output = tm.content[0]
                        if isinstance(tool_output, dict) and "text" in tool_output:
                            print(f"{Fore.MAGENTA}Tool Name: {Fore.WHITE}{tm.name}{Fore.MAGENTA} | Tool Result: {Fore.WHITE}{tool_output['text']}{Style.RESET_ALL}")
                        else:
                            print(f"{Fore.MAGENTA}Tool Name: {Fore.WHITE}{tm.name}{Fore.MAGENTA} | Tool Result: {Fore.WHITE}{tool_output}{Style.RESET_ALL}")
                    else:
                        print(f"{Fore.MAGENTA}Tool Name: {Fore.WHITE}{tm.name}{Fore.MAGENTA} | Tool Result: {Fore.WHITE}{tm.content}{Style.RESET_ALL}")

        except Exception as e:
            print(f"{Fore.RED}{Style.BRIGHT}Error: {Fore.WHITE}{e}{Style.RESET_ALL}")
    return