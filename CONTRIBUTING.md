# Contributing

Thanks for taking a look. Small, focused pull requests are easiest to review.

## Before you open a PR

```bash
python -m py_compile app/main.py
python scripts/check_frontend.py app/main.py
```

Both must pass. The second one runs `node --check` over the inline JavaScript; without it a stray
bracket will ship a page that loads but does nothing.

## Adding a new source

1. Implement three functions next to the existing ones in `app/main.py`:
   - `probe_x(input) -> {"title", "pages"}`
   - `run_x(job, img_dir, log_path) -> [image paths]` (write images into `img_dir`)
   - `x_search(q, by, page) -> [{"platform", "id", "title", "author", "url"}]`
2. Register them in the `PROBES` / `RUNNERS` dicts and in the `search()` dispatch.
3. Teach `parse_input()` the new input format.
4. Add an `<option>` to the search platform `<select>` in the `PAGE` template, plus an entry in
   the `I18N` dictionaries for that option.
5. Add the platform tag colour (`.tag.xx`) and a `PF` entry in `jobCard()`.

Please keep the download loop cancellable (`if job.get("cancel"): return []`) and update
`job["progress"]` / `job["message"]` as you go — the UI is driven entirely by those fields.

## Style

- Python: standard library formatting, no hard-coded paths (everything goes through
  `MANGA_HOME`), no secrets in code.
- User-visible strings: English in code; add translations to the `I18N` table for the UI.
- Error messages should say what failed and what the user can do about it.

## Reporting bugs

Include: source, input, the job message shown in the UI, and the relevant lines from
`journalctl -u manga -n 200`.
