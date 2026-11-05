# Helping with CodeMonkey

Thanks for wanting to help. Bug reports and ideas are welcome as issues; code is welcome as pull
requests.

## Getting it running

You need Python 3.10 or newer, and Node.js for the desktop window. The Python side uses the standard
library only.

```bash
python webui.py --project path/to/a/project     # then open http://127.0.0.1:8080
cd desktop && npm install && npm start           # or in its own window
```

Connect a model in Settings > Models (or on the start screen).

## Before you open a pull request

- Run the tests: `python -m pytest tests kernel/tests`. Most use a fake model, so no key or network
  is needed. The browser tests need Playwright and Chrome, and skip themselves without them.
- `python doctor.py` checks how the parts are wired together; it should print no FAIL.
- Keep the Python side free of third-party packages. (The Claude client uses the `anthropic`
  package when it is installed, and Anthropic's OpenAI-compatible endpoint when it is not.)
- Match the code around you: its naming, its comments, its plain words. On screen, say what a thing
  does in everyday language rather than its technical name.
- One change per pull request, with a sentence on why.

## How the code is laid out

The README's "How it is put together" table is the map. The app's page lives in `webui/static`
(plain JavaScript files that share one scope, loaded in order by `index.html`); the server in
`webui/`.

## License

CodeMonkey is public domain under the Unlicense. By sending a change you agree it is released the
same way.
