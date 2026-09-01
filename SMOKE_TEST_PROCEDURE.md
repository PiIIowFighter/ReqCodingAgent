# Live Smoke Test Procedure for Interactive Interview Feature

## Prerequisites

- CHATANYWHERE_API_KEY environment variable set
- Empty workspace directory: `/tmp/reqagent_interview_smoke`
- Python environment with all dependencies

## Step 1: Start GUI Server

```powershell
cd D:\Desktop\科研\.南大科研项目
$env:OPENAI_BASE_URL = "https://api.chatanywhere.tech/v1"
$env:OPENAI_API_KEY = $env:CHATANYWHERE_API_KEY

python demo_gui/server.py --host 127.0.0.1 --port 8765 --config configs/agent/demo-chatanywhere.json --artifact-root artifacts/runs/demo-gui
```

## Step 2: Open Browser

Navigate to: http://127.0.0.1:8765

## Step 3: Set Workspace

1. Click "修改工作目录" button
2. Enter: `/tmp/reqagent_interview_smoke` (or Windows equivalent: `C:\temp\reqagent_interview_smoke`)
3. Click "确认"
4. Verify workspace is accepted (directory is empty, should be valid)

## Step 4: Submit Vague Task

In the task input area, enter:

```
生成一个股票搜索网站
```

Press Ctrl+Enter or click send button.

## Expected Behavior: Interview Phase

### Turn 1: First Question
- Status badge shows: "访谈中" or "等待回答"
- Interview card appears with:
  - "需求访谈进行中" header
  - "问题 1 / 最多 3 轮" turn indicator
  - First question (should ask about tech stack, data source, or key features)
  - Associated ontology slot chips displayed
  - Answer textarea and "提交回答" button

**Test Answer 1:**
```
使用本地模拟股票数据，不需要真实股票 API。
```

Submit and wait for next question (status changes to "访谈中" briefly).

### Turn 2: Second Question
- Status returns to "等待回答"
- Question history shows Q1 and A1
- New question appears (should build on previous answer, e.g., ask about UI, search features, validation)
- Turn indicator shows "问题 2 / 最多 3 轮"

**Test Answer 2:**
```
支持按股票代码或名称搜索，结果显示代码、名称、当前价格和涨跌幅；没有匹配结果时给出提示。
```

Submit and wait.

### Turn 3: Third Question
- Status returns to "等待回答"
- Question history shows Q1/A1 and Q2/A2
- New question appears (final clarification, e.g., technology choice, display style)
- Turn indicator shows "问题 3 / 最多 3 轮"

**Test Answer 3:**
```
使用原生 HTML、CSS 和 JavaScript，采用简洁的深色界面，直接打开 index.html 即可使用。
```

Submit and wait.

### Baseline Confirmation
After Turn 3 answer:
- Status changes to "待确认"
- Interview card disappears
- Baseline card appears with:
  - "需求基线待确认" header
  - Refined summary (Chinese, concise description)
  - Functional requirements list (at least 2-3 items)
  - Acceptance criteria (testable conditions)
  - Technical constraints (native HTML/CSS/JS, no API)
  - Excluded scope (items user said not needed)
  - Assumptions (defaults when user was uncertain)
  - Unresolved items (if any)
  - "确认需求并开始编码" button

**Verification at this point:**
1. Check `/tmp/reqagent_interview_smoke` - should still be EMPTY
2. No files created yet
3. No subprocess started yet

Click "确认需求并开始编码" button.

## Expected Behavior: Coding Phase

### Coding Agent Execution
- Status changes to "Running"
- Baseline card disappears
- Timeline shows coding agent events:
  - Tool calls: list_files, read_file (if checking workspace)
  - apply_patch events (creating index.html, styles, data files)
  - run_command events (if testing in browser or with Node)
  - Multiple model_response events

### Completion
- Status changes to "Completed"
- Result card appears with:
  - "Agent finished" title (if stop_reason=submitted)
  - Result summary
  - Patch statistics (files created, lines added)
  - Download patch button
  - Patch preview

## Step 5: Verification

### Check Workspace
```bash
ls -la /tmp/reqagent_interview_smoke
```

Expected files:
- `index.html` (main page with search form)
- CSS file (styles for dark theme)
- JS file (search logic, mock data)
- Possibly additional files (data.json, README, etc.)

### Check Artifacts
```bash
ls -la artifacts/runs/demo-gui/interview-*
```

Expected files:
- `interview-<task_id>/interview-transcript.json`
- `interview-<task_id>/confirmed-requirement-baseline.json`
- `interview-<task_id>/final-task.txt`

### Verify Transcript Content

```bash
cat artifacts/runs/demo-gui/interview-*/interview-transcript.json | grep -E "ontology_version|original_request|completed"
```

Should show:
- `ontology_version`: SHA256 hash
- `original_request`: "生成一个股票搜索网站"
- `completed`: true
- 3 turns with questions and answers
- No absolute paths (C:\, D:\, /home/, /opt/)
- No API keys
- No "reasoning" or "encrypted_content"

### Verify Baseline Content

```bash
cat artifacts/runs/demo-gui/interview-*/confirmed-requirement-baseline.json | grep -E "configured_model|actual_model|refined_summary"
```

Should show:
- `configured_model`: "gpt-4o-mini"
- `actual_model`: "gpt-4o-mini" (or similar)
- `refined_summary`: Coherent Chinese summary
- `confirmed_at`: ISO timestamp

### Verify Final Task File

```bash
cat artifacts/runs/demo-gui/interview-*/final-task.txt
```

Should contain:
- # Original Request
- # Refined Summary
- # Requirements (with user's confirmed items)
- # Acceptance Criteria
- # Constraints (native tech, no API)
- # Excluded Scope
- # Assumptions
- Well-formatted, readable

### Verify Run Artifact

Check the main run artifact:
```bash
ls -la artifacts/runs/demo-gui/run-*
```

Should have:
- events.jsonl
- patch.diff
- submitted.json (if stop_reason=submitted)

### Test Created Website

Open `index.html` in browser:
```bash
# Windows
start /tmp/reqagent_interview_smoke/index.html

# Unix
open /tmp/reqagent_interview_smoke/index.html
```

Manual verification:
- Dark theme applied
- Search input and button visible
- Can enter stock code (e.g., "AAPL", "000001")
- Search displays mock data (name, price, change)
- Invalid search shows error message
- No external API calls (check browser console)

## Success Criteria

✅ Interview started automatically for vague task
✅ At least 2 questions asked
✅ Questions influenced by previous answers
✅ Each question associated with real ontology slots
✅ Workspace remained empty during interview
✅ No subprocess started before confirmation
✅ Baseline card displayed all sections
✅ User confirmation triggered coding agent
✅ Configured model was gpt-4o-mini
✅ Actual model recorded
✅ Files created in workspace after confirmation
✅ At least one apply_patch and one run_command
✅ Patch non-empty
✅ stop_reason=submitted
✅ GUI showed all phases correctly
✅ No absolute paths in transcript or baseline
✅ No API keys leaked
✅ No reasoning or encrypted_content in artifacts
✅ Created website functions as described

## Failure Cases

If any of these occur, document and DO NOT retry:

❌ Interview skipped, went directly to coding
❌ Less than 2 questions asked
❌ Questions generic, not influenced by answers
❌ Files created before user confirmation
❌ Subprocess started during interview phase
❌ Model used was not gpt-4o-mini
❌ Actual model not recorded
❌ stop_reason != "submitted"
❌ Absolute paths in transcript
❌ API keys visible in any artifact
❌ reasoning/encrypted_content leaked

## Clean Up

After verification:
```bash
rm -rf /tmp/reqagent_interview_smoke/*
```

## Notes

- This is the ONLY allowed live smoke test for this development session
- Must use gpt-4o-mini throughout
- No retries on failure
- No budget increases
- No model changes
- Preserve all artifacts as evidence
