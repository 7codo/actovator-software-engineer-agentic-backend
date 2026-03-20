from langchain.tools import tool


@tool
def get_phase_1_context() -> str:
    """
    Returns instructions for Phase 1: Context (read-only discovery).
    """
    return """
## PHASE 1 — Context (read-only)

Gather all necessary context using available tools. Do not modify any state.

---

### Conditions
- Use the available tools to gather all necessary context. Proceed only when you are certain you have sufficient information.

### Rules
- No state modifications allowed in this phase.

### Acceptance Criteria
- [ ] The files/resources involved are identified
- [ ] The current values or states are known
- [ ] The constraints on the planned action are understood
"""


@tool
def get_phase_2_execute() -> str:
    """
    Returns instructions for Phase 2: Execute (state-modifying actions).
    """
    return """
## PHASE 2 — Execute (state-modifying)

Execute state-modifying tool calls one at a time. Justify and verify each call.

---

### Conditions
- All Phase 1 acceptance criteria are met and the task requires at least one state-modifying tool call. Skip this phase entirely if the task is purely informational.

### Rules
- Before each tool call: state the specific action and the Phase 1 evidence that justifies it.
- Before each tool call: state the expected outcome as a concrete, checkable signal.

### Acceptance Criteria
- [ ] No error was returned by the tool
- [ ] Use symbolic editing tools whenever possible for precise code modifications.
"""


@tool
def get_failure_protocol() -> str:
    """
    Returns instructions for classifying and recovering from tool call failures.
    """
    return """
## FAILURE PROTOCOL

Classify the failure type, then follow the matching recovery action.

---

### Conditions
- A Phase 2 tool call returned an error or produced an unexpected result.
- Classify the failure before taking any recovery action.

### Rules
- Never retry an identical failing call — every retry must reflect a concrete change.
- Classify first, act second — do not skip classification.

### Classification Table
| Type             | Signal                        | Recovery                                       |
|------------------|-------------------------------|------------------------------------------------|
| Missing context  | Cannot determine what to do   | Return to Phase 1 with a narrower query        |
| Wrong parameters | Tool returned an error        | Re-call get_params_tool, correct params, retry |
| Stale context    | State changed between phases  | Re-run Phase 1, then retry Phase 2             |
| Unreachable      | All recovery paths exhausted  | Call get_escalation_report()                   |

### Acceptance Criteria
- [ ] The failure type is classified against the table above
- [ ] The chosen recovery action matches the classified type
- [ ] The retry (if any) reflects a concrete change from the failed call
"""


@tool
def get_escalation_report() -> str:
    """
    Returns instructions for producing a structured escalation report.
    """
    return """
## ESCALATION REPORT

Produce a structured three-section report. Do not attempt any further recovery.

---

### Conditions
- The failure type has been classified as Unreachable and all recovery paths are exhausted. 
- Produce this report before calling get_verification().

### Rules
- No speculation — every claim must be backed by an observed tool output.
- Do not attempt further recovery actions at this stage.

### Report Structure
- Completed: what was successfully executed (tool calls made, outcomes observed)
- Incomplete: what remains unfinished and what the next step would have been
- Blocked: the specific, evidence-based reason completion is unreachable

### Acceptance Criteria
- [ ] All three report sections are present and populated
- [ ] Every claim in the Blocked section references an observed tool output
- [ ] No further tool calls are attempted after the report is produced
"""


@tool
def get_verification() -> str:
    """
    Returns instructions for triggering the final verification process.
    """
    return """
## VERIFICATION

Call start_verification_process immediately. This is always the final step.

---

### Conditions
- The task has reached its final state — whether successfully executed, purely informational, or ended with an escalation report.

### Rules
- Do NOT call get_params_tool for start_verification_process — its parameters
  are derived directly from user_task and execution_result.
- No further tool calls are made after start_verification_process.

### Acceptance Criteria
- [ ] start_verification_process is called with user_task and execution_result
- [ ] execution_result contains a full summary of what was done and what was observed
- [ ] No tool calls are made after this step
"""
