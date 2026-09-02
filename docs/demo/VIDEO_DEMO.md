# Two-minute stock-search video demo

This is a presentation procedure, not a benchmark. The frozen ontology remains domain-neutral; `stock-search.json` is a read-only example mapping and does not add slots or change routing.

## Prepare once

Use a neutral path such as `D:\ReqAgentDemo\stock-search-final` so the recording does not expose a personal directory. The target must be missing or empty.

```powershell
py -3.11 demo_gui/prepare_stock_demo.py D:\ReqAgentDemo\stock-search-final
docker info

$recordingConfig = Join-Path $env:TEMP "reqagent-video-gpt56.json"
$config = Get-Content configs/agent/demo-openai.json -Raw | ConvertFrom-Json
$config.model.model = "gpt-5.6-sol"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($recordingConfig, ($config | ConvertTo-Json -Depth 20), $utf8NoBom)
```

Set `OPENAI_BASE_URL` and `OPENAI_API_KEY` in the shell without displaying their values. Start the static preview and GUI in separate terminals:

```powershell
py -3.11 -m http.server 8088 --directory D:\ReqAgentDemo\stock-search-final
py -3.11 -m demo_gui.server --host 127.0.0.1 --port 8765 --workspace D:\ReqAgentDemo\stock-search-final --config $recordingConfig --demo-scenario stock-search
```

Open `http://127.0.0.1:8088` and `http://127.0.0.1:8765` before recording. Do one private rehearsal first; use a newly prepared directory for the final take.

## Recorded task

Submit: `为这个现有的静态前端项目增加股票搜索功能`

Answer the three dynamically generated questions with these facts, adapting the wording to the actual question:

1. 使用内置的本地模拟股票数据，不接入真实股票 API，也不需要联网。
2. 支持按股票代码或中文名称搜索，展示代码、名称、当前价格和涨跌幅；空输入显示全部，没有匹配结果时给出明确提示。
3. 保留现有原生 HTML、CSS 和 JavaScript，不新增依赖，采用简洁的深色界面；完成后运行 `sh test_site.sh`，并能直接打开 `index.html` 使用。

Confirm the generated requirement baseline, then let the Agent continue. The expected visible sequence is:

`需求识别 → 主动澄清 → 需求基线 → 仓库调查 → 代码修改 → 验证 → 完成`

The timeline should show real `list_files` / `read_file` / `search_text`, `apply_patch`, `run_command` and `submit` events. Final acceptance is `stop_reason=submitted`, a non-empty patch, a visible successful `sh test_site.sh`, and a working search page at `http://127.0.0.1:8088`.

## Recording boundary

Keep the final video under two minutes. Accelerate or cut model waiting time without changing event order. Never show environment-variable values, credentials, personal paths, hidden reasoning, raw encrypted content, or failed rehearsal artifacts.
