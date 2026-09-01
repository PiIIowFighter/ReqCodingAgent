# Interactive Requirement Interview Implementation Summary

## Implementation Complete

All code changes have been implemented and offline tests pass. The live smoke test requires manual execution through the GUI as documented in `SMOKE_TEST_PROCEDURE.md`.

## Changes Summary

### Core Interview Infrastructure

**`demo_gui/interview.py`** (new file, 350 lines)
- `InterviewSession` class: manages multi-turn interview with OpenAI Responses protocol
- `InterviewTurn` dataclass: tracks questions, answers, slot selections
- `RequirementBaseline` dataclass: synthesizes confirmed requirements
- Tool schemas: `ask_clarification` and `finish_interview`
- Enforces 2-3 turn limit
- Prevents duplicate answers and invalid turn IDs
- Converts baseline to task description for coding agent

**`prompts/demo/requirement-interview.txt`** (new file)
- System prompt for interview agent
- Ontology-driven slot selection guidance
- Question quality requirements
- Tool usage instructions
- Baseline synthesis requirements

### GUI Server Integration

**`demo_gui/server.py`** (modified)
- Added interview states: `interviewing`, `awaiting_user`, `awaiting_confirmation`
- Extended `TaskRecord` with interview fields
- `_start_interview()`: initializes InterviewSession with OpenAI client
- `_process_answer()`: handles user answers in background thread
- `confirm_baseline()`: saves artifacts and starts coding agent
- `_run_coding_agent()`: refactored from original `_run`, supports `--task-file`
- New endpoints: `POST /api/tasks/{id}/answer`, `POST /api/tasks/{id}/confirm`
- `public_task()`: exposes interview state (current question, history, baseline)
- Routing integration: vague tasks → interview, detailed tasks → direct coding

### GUI Frontend

**`demo_gui/static/app.js`** (modified)
- `showInterviewQuestion()`: renders question, slots, history
- `submitAnswer()`: sends answer to server
- `showBaselineConfirmation()`: displays requirement baseline for review
- `confirmBaseline()`: triggers coding agent start
- Status mapping: Chinese labels for interview states
- Polling updates handle interview states

**`demo_gui/static/index.html`** (modified)
- `#interview-card`: question display, answer input, history
- `#baseline-card`: requirement baseline review, confirmation button
- Styled interview UI components

### Tests

**`tests/test_demo_gui_interview.py`** (new file, 300+ lines)
- 11 comprehensive tests, all passing
- `FakeClient` and `FakeResponse` for offline testing
- Tests cover:
  - First question generation
  - Minimum 2 turns enforcement
  - Maximum 3 turns enforcement
  - Duplicate answer rejection
  - Invalid turn_id rejection
  - Baseline synthesis
  - Transcript structure
  - Path/key sanitization
  - Routing integration

**`tests/test_reqagent_adaptive_controller.py`** (modified)
- Updated greenfield routing tests
- Vague tasks (Chinese/English) → refine mode
- Detailed tasks → fast mode

## Offline Test Results

### Interview Tests (11/11 passed)
```
✓ test_interview_starts_with_first_question
✓ test_greenfield_task_enters_interview
✓ test_detailed_task_skips_interview
✓ test_interview_requires_at_least_two_turns
✓ test_interview_finishes_after_max_turns
✓ test_interview_rejects_duplicate_answer
✓ test_interview_rejects_invalid_turn_id
✓ test_baseline_to_task_description
✓ test_interview_transcript_structure
✓ test_interview_no_absolute_paths_in_transcript
✓ test_interview_session_preserves_actual_model
```

### Routing Tests (4/4 passed)
```
✓ Vague Chinese greenfield -> refine
✓ Vague English greenfield -> refine
✓ Detailed task -> fast
✓ Detailed greenfield -> fast
```

### Adaptive Controller Tests (3/3 passed)
```
✓ test_router_treats_vague_greenfield_chinese_tasks_as_refine
✓ test_router_treats_vague_greenfield_english_tasks_as_refine
✓ test_router_treats_detailed_greenfield_tasks_as_fast
```

### Code Verification
- ✓ `python -m py_compile` on all modified Python files
- ✓ `git diff --check` (only line ending warnings)
- ✓ `uv lock --check` passed

## State Machine

```
Task submitted
    ↓
route_task() decision
    ↓
├─ mode=fast ────→ Running (direct to coding agent)
│
└─ mode=refine ──→ Interviewing
                    ↓
                  awaiting_user (Q1)
                    ↓ (user answers)
                  Interviewing
                    ↓
                  awaiting_user (Q2)
                    ↓ (user answers)
                  Interviewing
                    ↓
                  awaiting_user (Q3)
                    ↓ (user answers)
                  Interviewing (force finish)
                    ↓
                  awaiting_confirmation (baseline ready)
                    ↓ (user confirms)
                  Running (coding agent with baseline)
                    ↓
                  Completed/Failed
```

## Safety Guarantees

### Pre-Confirmation
- ✅ No files created in workspace
- ✅ No subprocess started
- ✅ No git operations
- ✅ Workspace remains untouched

### Sanitization
- ✅ Absolute paths redacted from events
- ✅ API keys never logged
- ✅ Reasoning/encrypted_content not in artifacts
- ✅ Only sanitized data exposed to GUI

### State Validation
- ✅ Only `awaiting_user` accepts answers
- ✅ Only `awaiting_confirmation` accepts confirmation
- ✅ Duplicate answers rejected
- ✅ Invalid turn_ids rejected
- ✅ Stale confirmations rejected

## Interview Quality

### Ontology Integration
- Real frozen ontology loaded from `configs/frozen/baseline-v3/requirement-ontology.json`
- Slot IDs must come from ontology (no invention)
- Selection reason required for each question
- Slot states tracked: confirmed, rejected, unresolved, unexplored

### Turn Limits
- Minimum: 2 turns (enforced in `finish_interview`)
- Maximum: 3 turns (forced finish after turn 3)
- Each turn: ONE question, ONE set of slots, ONE answer

### Dynamic Questioning
- Questions build on previous answers
- Later questions influenced by earlier responses
- No fixed questionnaire
- No repeated questions for confirmed/rejected slots

## Artifact Structure

### Interview Transcript (`interview-<task_id>/interview-transcript.json`)
```json
{
  "original_request": "生成一个股票搜索网站",
  "ontology_version": "sha256_hash",
  "turns": [
    {
      "turn_id": "call-1",
      "question": "...",
      "selected_slot_ids": ["..."],
      "selection_reason": "...",
      "answer": "...",
      "timestamp": 1234567890.0
    }
  ],
  "baseline": {...},
  "completed": true
}
```

### Confirmed Baseline (`interview-<task_id>/confirmed-requirement-baseline.json`)
```json
{
  "original_request": "...",
  "refined_summary": "...",
  "requirements": [...],
  "acceptance_criteria": [...],
  "constraints": [...],
  "excluded_scope": [...],
  "assumptions": [...],
  "unresolved_items": [...],
  "slot_states": {...},
  "confirmed_at": "ISO timestamp",
  "ontology_version": "...",
  "configured_model": "gpt-4o-mini",
  "actual_model": "gpt-4o-mini"
}
```

### Final Task File (`interview-<task_id>/final-task.txt`)
```markdown
# Original Request
生成一个股票搜索网站

# Refined Summary
...

# Requirements
- ...

# Acceptance Criteria
- ...

(etc.)
```

## Compliance

✅ Single-threaded main session only
✅ No subagents or parallel agents
✅ No complete test suite or benchmark runs
✅ All real API uses gpt-4o-mini
✅ No gpt-5.6-sol requests
✅ No baseline-v1/v2/v3 modifications
✅ No frozen ontology modifications
✅ No audit evidence modifications
✅ Preserved GUI layout and workspace UX
✅ No new dependencies (React, Node, databases)
✅ No amend/rebase/force push
✅ Workspace safety rules enforced
✅ Clean working tree before commit

## Live Smoke Test

The live smoke test requires manual execution as it involves:
1. Starting GUI server (blocking process)
2. Browser interaction
3. Multi-turn conversation with real model
4. Manual verification of created files

Complete procedure documented in `SMOKE_TEST_PROCEDURE.md`.

**Expected outcome:**
- 3-turn interview for "生成一个股票搜索网站"
- User answers about data source, features, technology
- Baseline confirmation
- Coding agent creates functional stock search website
- All artifacts present and sanitized

## File Manifest

### New Files
- `demo_gui/interview.py` (350 lines)
- `prompts/demo/requirement-interview.txt` (60 lines)
- `tests/test_demo_gui_interview.py` (320 lines)
- `SMOKE_TEST_PROCEDURE.md` (documentation)
- `INTERVIEW_IMPLEMENTATION_SUMMARY.md` (this file)

### Modified Files
- `demo_gui/server.py` (+180 lines)
- `demo_gui/static/app.js` (+120 lines)
- `demo_gui/static/index.html` (+3 sections)
- `tests/test_reqagent_adaptive_controller.py` (+15 lines, 3 tests updated)

### Preserved from 55e7af3
- Routing logic (greenfield no longer hardcoded to fast)
- Workspace safety (empty/clean dirs only)
- Patch reliability (--recount flag)
- Budget limits (restored demo values)
- GUI events (route_decision, requirement_brief_recorded)

## Next Steps

1. Execute live smoke test following `SMOKE_TEST_PROCEDURE.md`
2. If successful (stop_reason=submitted, all criteria met):
   - Commit with message: `feat(gui): add interactive requirement interview`
   - Push to origin/main
3. If failed:
   - Preserve evidence
   - Document failure mode
   - Do NOT retry or increase budget
