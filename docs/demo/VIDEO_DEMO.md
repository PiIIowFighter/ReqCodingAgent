# Stock-search video demo

Prepare a fixture with `python demo_gui/prepare_stock_demo.py <empty-target>`, then start the GUI with `python -m demo_gui.server --workspace <target> --demo-scenario stock-search`.

Use the fixed task: `为这个现有的静态前端项目增加股票搜索功能`. Answer three questions manually:

1. 使用内置的本地模拟股票数据，不接入真实股票 API，也不需要联网。
2. 支持按股票代码或中文名称搜索，展示代码、名称、当前价格和涨跌幅；空输入显示全部，没有匹配结果时给出明确提示。
3. 保留现有原生 HTML、CSS 和 JavaScript，不新增依赖，采用简洁的深色界面；完成后运行 sh test_site.sh，并能直接打开 index.html 使用。

Record the route, coverage panel, investigation events, patch, successful `sh test_site.sh`, and submitted result. The fixture starts as a working stock list without search, so the Agent must inspect and incrementally modify it. Waiting for the model may be accelerated or cut in editing, but event order must remain unchanged. Do not show personal paths, keys, environment values, or hidden reasoning.

Final acceptance: three-turn refine interview, visible slot coverage, real `list_files`/`read_file`/`search_text`/`record_requirement_brief`/`apply_patch`/`run_command`/`submit` events, non-empty patch whose preview equals its download, and a copied workspace passing `sh test_site.sh`.
